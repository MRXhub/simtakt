#!/usr/bin/env python3
"""Sandbox-runnable contract tests for the repository overview projection."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from control_plane.core.evaluation_contracts import (
    make_candidate,
    make_evaluation_request,
    make_problem_definition,
)
from control_plane.data.sqlite_evaluation_repository import SQLiteEvaluationRepository
from control_plane.evaluation.service import EvaluationMiddleware
from control_plane.web.status_server import _HttpError, parse_overview_limit

REV_A = "sha256:" + "1" * 64
REV_B = "sha256:" + "2" * 64
REV_C = "sha256:" + "3" * 64
STATUSES = {
    "requested", "deduplicating", "queued", "running", "recovering",
    "qualifying", "qualified", "ambiguous", "unresolved", "cancelled",
}
STUDY_KEYS = {
    "study_id", "problem_id", "problem_revision", "created_at",
    "algorithm_run_id", "automation_profile", "evaluation_count",
    "status_counts", "active_count", "waiting_count", "oldest_wait",
    "last_activity_at",
}


class StudyOverviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.middleware = EvaluationMiddleware(
            SQLiteEvaluationRepository(Path(self.tmp.name) / "control.sqlite3")
        )
        problem = make_problem_definition(
            problem_id="overview-problem", parameter_schema_revision=REV_A,
            constraint_revision=REV_B, simulation_capabilities=["simulation"],
            metric_schema_revision=REV_C,
        )
        self.middleware.register_problem(problem)
        self.problem = problem
        self.clock = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # An empty study is important: it has no activity timestamp.
        self.study("empty", automation_profile="manual", algorithm_run_id="run:empty")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def study(self, name: str, **kwargs) -> str:
        study_id = "study:" + name
        self.middleware.create_study(
            study_id=study_id, problem_id=self.problem["problem_id"],
            problem_revision=self.problem["revision"], **kwargs,
        )
        return study_id

    def evaluation(self, name: str, study_id: str) -> dict:
        candidate = make_candidate(
            problem_id=self.problem["problem_id"],
            problem_revision=self.problem["revision"], parameters={"x": name},
        )
        request = make_evaluation_request(
            candidate_id=candidate["candidate_id"], fidelity="atlas",
            requested_outputs=["iv"], evidence_profile="default",
        )
        return self.middleware.submit(candidate, request, study_id=study_id)

    def attempt(self, evaluation_id: str, number: str, when: datetime) -> dict:
        planned = self.middleware._repository.schedule_attempt(
            evaluation_id=evaluation_id, simulation_adapter="fixture-adapter",
            numerical_profile="fixture-profile", attempt_id="attempt:" + str(uuid.uuid4()),
        )
        leased = self.middleware._repository.lease_next_attempt("worker:" + number, 600, now=when)
        self.assertIsNotNone(leased)
        assert leased is not None
        self.middleware._repository.start_attempt(
            planned["attempt_id"], "worker:" + number, now=when + timedelta(seconds=1)
        )
        return planned

    def test_overview_contract_waiting_counts_ordering_and_limit(self) -> None:
        requested = self.evaluation("requested", self.study("requested"))

        running_study = self.study("running", automation_profile="autonomous")
        running = self.evaluation("running", running_study)
        self.attempt(running["evaluation_id"], "running", self.clock)

        recovering_study = self.study("recovering")
        recovering = self.evaluation("recovering", recovering_study)
        failed = self.attempt(recovering["evaluation_id"], "recovering", self.clock + timedelta(seconds=10))
        self.middleware.fail_attempt(
            failed["attempt_id"], "worker:recovering", "license-timeout",
            now=self.clock + timedelta(seconds=12),
        )

        queued_study = self.study("queued")
        queued = self.evaluation("queued", queued_study)
        failed_queued = self.attempt(queued["evaluation_id"], "queued", self.clock + timedelta(seconds=20))
        self.middleware.fail_attempt(
            failed_queued["attempt_id"], "worker:queued", "solver-crash",
            now=self.clock + timedelta(seconds=22),
        )
        self.middleware.operator_requeue(
            queued["evaluation_id"], "retry", now=self.clock + timedelta(seconds=23)
        )

        # A reconciling attempt has a wait reason but deliberately no wait_since.
        recon_study = self.study("reconciling")
        recon = self.evaluation("reconciling", recon_study)
        recon_attempt = self.attempt(recon["evaluation_id"], "reconciling", self.clock + timedelta(seconds=30))
        self.middleware.require_reconciliation(
            recon_attempt["attempt_id"], "worker:reconciling", reason="uncertain",
            now=self.clock + timedelta(seconds=31),
        )

        result = self.middleware.study_overviews()
        self.assertEqual(result["study_count"], 6)
        self.assertEqual(len(result["studies"]), 6)
        by_id = {item["study_id"]: item for item in result["studies"]}
        for item in result["studies"]:
            self.assertEqual(set(item), STUDY_KEYS)
            self.assertTrue(set(item["status_counts"]).issubset(STATUSES))

        self.assertEqual(by_id["study:empty"]["evaluation_count"], 0)
        self.assertIsNone(by_id["study:empty"]["last_activity_at"])
        self.assertEqual(by_id["study:empty"]["problem_id"], "overview-problem")
        self.assertEqual(by_id["study:empty"]["algorithm_run_id"], "run:empty")
        self.assertEqual(by_id["study:empty"]["automation_profile"], "manual")
        self.assertEqual(by_id["study:requested"]["status_counts"], {"queued": 1})
        self.assertEqual(by_id["study:requested"]["active_count"], 1)
        self.assertEqual(by_id["study:recovering"]["waiting_count"], 1)
        self.assertEqual(by_id["study:recovering"]["oldest_wait"]["wait_reason"], "license-timeout")
        self.assertEqual(by_id["study:queued"]["waiting_count"], 1)
        self.assertEqual(by_id["study:queued"]["oldest_wait"]["wait_reason"], "requeued-after:solver-crash")
        self.assertEqual(by_id["study:reconciling"]["oldest_wait"]["wait_since"], None)
        self.assertEqual(by_id["study:reconciling"]["oldest_wait"]["wait_reason"], "reconciling")

        ordered_items = result["studies"]
        self.assertTrue(all(item["active_count"] > 0 for item in ordered_items[:-1]))
        self.assertEqual(ordered_items[-1]["study_id"], "study:empty")
        self.assertEqual(
            [item["study_id"] for item in self.middleware.study_overviews(1)["studies"]],
            [ordered_items[0]["study_id"]],
        )
        self.assertEqual(self.middleware.study_overviews(2)["study_count"], 6)
        self.assertEqual(len(self.middleware.study_overviews(2)["studies"]), 2)


class OverviewLimitParserTests(unittest.TestCase):
    def test_parse_overview_limit_contract(self) -> None:
        self.assertIsNone(parse_overview_limit(""))
        self.assertEqual(parse_overview_limit("foo=bar"), None)
        self.assertEqual(parse_overview_limit("limit=7"), 7)
        for query in ("limit=0", "limit=-1", "limit=abc", "limit=", "limit=1&limit=2"):
            with self.assertRaises(_HttpError) as error:
                parse_overview_limit(query)
            self.assertEqual(error.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
