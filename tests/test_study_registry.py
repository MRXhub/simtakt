#!/usr/bin/env python3
"""Study registry and evaluation membership tests."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from control_plane.core.evaluation_contracts import (
    ContractError,
    make_candidate,
    make_evaluation_request,
    make_problem_definition,
)
from control_plane.data.sqlite_evaluation_repository import RepositoryError, SQLiteEvaluationRepository
from control_plane.evaluation.service import EvaluationMiddleware


REV_A = "sha256:" + "1" * 64
REV_B = "sha256:" + "2" * 64
REV_C = "sha256:" + "3" * 64


class StudyRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Path(self.tmp.name) / "control.sqlite3"
        self.middleware = EvaluationMiddleware(SQLiteEvaluationRepository(self.database))
        from tests.shared_fixtures import register_fixture_schema
        schema_revision = register_fixture_schema(self.middleware, problem_hint="study")
        self.problem = make_problem_definition(
            problem_id="study-problem",
            parameter_schema_revision=schema_revision,
            constraint_revision=REV_B,
            simulation_capabilities=["simulation"],
            metric_schema_revision=REV_C,
        )
        self.middleware.register_problem(self.problem)
        self.candidate = make_candidate(
            problem_id=self.problem["problem_id"],
            problem_revision=self.problem["revision"],
            parameters={"x": 1},
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def request(self):
        return make_evaluation_request(
            candidate_id=self.candidate["candidate_id"],
            fidelity="atlas",
            requested_outputs=["iv"],
            evidence_profile="default",
        )

    def test_create_replay_conflict_and_validation(self) -> None:
        created = self.middleware.create_study(
            study_id="study-a",
            problem_id=self.problem["problem_id"],
            problem_revision=self.problem["revision"],
            metadata={"owner": "lab"},
            artifact_refs=[{"artifact_id": "design-a", "revision": REV_A}],
        )
        self.assertEqual(created, self.middleware.create_study(
            study_id="study-a",
            problem_id=self.problem["problem_id"],
            problem_revision=self.problem["revision"],
            metadata={"owner": "lab"},
            artifact_refs=[{"artifact_id": "design-a", "revision": REV_A}],
        ))
        with self.assertRaisesRegex(RepositoryError, "identity collision"):
            self.middleware.create_study(
                study_id="study-a",
                problem_id=self.problem["problem_id"],
                problem_revision=self.problem["revision"],
                metadata={"owner": "other"},
            )
        with self.assertRaises(RepositoryError):
            self.middleware.create_study(
                study_id="study-missing",
                problem_id=self.problem["problem_id"],
                problem_revision=REV_A,
            )
        with self.assertRaises(ContractError):
            self.middleware.create_study(
                study_id="not valid",
                problem_id=self.problem["problem_id"],
                problem_revision=self.problem["revision"],
            )

    def test_membership_submission_is_idempotent_and_cross_study_reuses_evaluation(self) -> None:
        for study_id in ("study-a", "study-b"):
            self.middleware.create_study(
                study_id=study_id,
                problem_id=self.problem["problem_id"],
                problem_revision=self.problem["revision"],
            )
        first = self.middleware.submit_evaluation(
            self.candidate, self.request(), study_id="study-a"
        )
        second = self.middleware.submit_evaluation(
            self.candidate, self.request(), study_id="study-b"
        )
        self.assertEqual(first["evaluation_id"], second["evaluation_id"])
        self.middleware.submit_evaluation(
            self.candidate, self.request(), study_id="study-a"
        )
        plain = make_evaluation_request(
            candidate_id=self.candidate["candidate_id"],
            fidelity="atlas",
            requested_outputs=["metric"],
            evidence_profile="default",
        )
        self.middleware.submit_evaluation(self.candidate, plain)
        with closing(sqlite3.connect(self.database)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM study_evaluations WHERE evaluation_id = ?",
                (first["evaluation_id"],),
            ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_status_and_problem_projections(self) -> None:
        self.middleware.create_study(
            study_id="study-a",
            problem_id=self.problem["problem_id"],
            problem_revision=self.problem["revision"],
        )
        evaluation = self.middleware.submit_evaluation(
            self.candidate, self.request(), study_id="study-a"
        )
        status = self.middleware.get_study_status("study-a")
        self.assertEqual(status["study"]["study_id"], "study-a")
        self.assertEqual(status["evaluations"][0]["evaluation_id"], evaluation["evaluation_id"])
        self.assertEqual(status["evaluations"][0]["attempts"], [])
        self.assertEqual(
            [item["study_id"] for item in self.middleware.list_studies(self.problem["problem_id"])],
            ["study-a"],
        )
        self.assertEqual(
            [item["evaluation_id"] for item in self.middleware.list_evaluations(self.problem["problem_id"])],
            [evaluation["evaluation_id"]],
        )
        self.assertEqual(
            self.middleware.list_problem_evaluations(
                self.problem["problem_id"], self.problem["revision"]
            )[0]["evaluation_id"],
            evaluation["evaluation_id"],
        )

    def test_study_none_and_unknown_study(self) -> None:
        request = self.request()
        plain = self.middleware.submit(self.candidate, request)
        listed = self.middleware.list_evaluations(self.problem["problem_id"])
        self.assertEqual(len(listed), 1)
        row = listed[0]
        for key, value in plain.items():
            self.assertEqual(row[key], value, key)
        self.assertEqual(row["problem_id"], self.problem["problem_id"])
        self.assertEqual(row["problem_revision"], self.problem["revision"])
        self.assertEqual(row["study_ids"], [])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM study_evaluations").fetchone()[0],
                0,
            )
        with self.assertRaisesRegex(RepositoryError, "unknown Study"):
            self.middleware.submit_evaluation(
                self.candidate,
                make_evaluation_request(
                    candidate_id=self.candidate["candidate_id"],
                    fidelity="atlas",
                    requested_outputs=["other"],
                    evidence_profile="default",
                ),
                study_id="does-not-exist",
            )


if __name__ == "__main__":
    unittest.main()
