from __future__ import annotations

import unittest
from uuid import uuid4

from control_plane.core.evaluation_contracts import ContractError
from control_plane.evaluation.service import EvaluationMiddleware
from control_plane.simulation.session_contracts import (
    make_simulation_session_plan,
    make_simulation_session_result,
    make_solver_run_record,
    validate_simulation_session_result,
)


class SolverRunRecordSessionTests(unittest.TestCase):
    def setUp(self):
        z = "sha256:" + "0" * 64
        self.plan = make_simulation_session_plan(
            attempt_id="attempt:" + str(uuid4()), evaluation_id="evaluation:" + str(uuid4()),
            candidate_id="candidate:sha256:" + "1" * 64, simulation_proxy="test",
            recovery_profile_revision=z, base_package_artifact_id="pkg", base_package_revision=z,
            task_id="task", target_id="target", authorization_id="auth", authorization_revision=z,
            requested_processors=1, command_timeout_seconds=1, max_solver_runs=1, max_wall_seconds=1,
        )
        self.kw = dict(plan_id=self.plan["plan_id"], attempt_id=self.plan["attempt_id"],
                       session_ref="session", status="completed", solver_run_record_ids=[],
                       journal_artifact_id="journal", evidence_artifact_ids=["evidence"])

    def test_session_result_absent_solver_run_records_is_accepted(self):
        run = self._run()
        result = make_simulation_session_result(
            **{**self.kw, "solver_run_record_ids": [run["record_id"]]}
        )
        self.assertEqual(validate_simulation_session_result(result), result)

    def test_session_result_rejects_unknown_solver_run_record_id(self):
        run = make_solver_run_record(
            plan_id=self.plan["plan_id"], sequence=1, run_id="run", package_artifact_id="pkg",
            package_revision="sha256:" + "0" * 64, numerical_profile_revision="sha256:" + "0" * 64,
            action="initial", status="completed", exit_code=0, artifact_ids=["a"], wall_seconds=1,
        )
        with self.assertRaises(ContractError):
            make_simulation_session_result(
                **{**self.kw, "solver_run_record_ids": ["solver-run-record:sha256:" + "f" * 64]},
                solver_run_records=[run],
            )
    

    def _run(self):
        return make_solver_run_record(
            plan_id=self.plan["plan_id"], sequence=1, run_id="run", package_artifact_id="pkg",
            package_revision="sha256:" + "0" * 64, numerical_profile_revision="sha256:" + "0" * 64,
            action="initial", status="completed", exit_code=0, artifact_ids=["a"], wall_seconds=2,
        )

    def test_terminal_feedback_fills_wall_seconds_from_records(self):
        worker = object.__new__(EvaluationMiddleware)
        out = worker._terminal_feedback(success=True, feedback={}, solver_run_records=[self._run()])
        self.assertEqual(out["wall_seconds"], 2.0)

    def test_terminal_feedback_explicit_feedback_wins(self):
        worker = object.__new__(EvaluationMiddleware)
        out = worker._terminal_feedback(success=True, feedback={"wall_seconds": 9}, solver_run_records=[self._run()])
        self.assertEqual(out["wall_seconds"], 9.0)


if __name__ == "__main__":
    unittest.main()
