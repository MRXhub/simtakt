from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "adapter-batch-queue"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(EXAMPLE) not in sys.path:
    sys.path.insert(0, str(EXAMPLE))

# The adapter.py module is the single normative import entry point for this example.
from adapter import BatchQueueWorker
from fake_queue import FakeBatchQueue

from control_plane.simulation.session_contracts import (
    make_simulation_session_plan,
    make_solver_run_record,
    normalize_artifact_id,
    validate_simulation_session_result,
    validate_solver_run_record,
)

class BatchQueueExampleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.queue = FakeBatchQueue()
        self.worker = BatchQueueWorker(self.queue)
        self.tmp = tempfile.TemporaryDirectory()
        self.allocation = {
            "remote_workspace_root": str(Path(self.tmp.name) / "workspace"),
            "processors": 2,
        }
        self.plan = make_simulation_session_plan(
            attempt_id="attempt:00000000-0000-4000-8000-000000000001",
            evaluation_id="evaluation:00000000-0000-4000-8000-000000000002",
            candidate_id="candidate:sha256:" + "a" * 64,
            simulation_proxy="batch-test",
            recovery_profile_revision="sha256:" + "b" * 64,
            base_package_artifact_id="artifact:pkg",
            base_package_revision="sha256:" + "c" * 64,
            task_id="task:test",
            target_id="target:test",
            authorization_id="artifact:auth",
            authorization_revision="sha256:" + "d" * 64,
            requested_processors=2,
            command_timeout_seconds=60,
            max_solver_runs=1,
            max_wall_seconds=60,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _start(self, ref: str = "session:test") -> str:
        self.worker.start_session(self.plan, self.allocation, ref)
        job_id = self.worker.job_id_for(ref)
        self.assertIsNotNone(job_id)
        return job_id  # type: ignore[return-value]

    def test_normal_lifecycle_returns_valid_result_and_artifact(self) -> None:
        ref = "session:normal"
        job_id = self._start(ref)
        self.assertEqual(self.worker.observe_session(ref), "running")

        self.queue.complete(job_id)
        self.assertEqual(self.worker.observe_session(ref), "completed")
        result, artifact_id = self.worker.collect_session(ref)

        self.assertEqual(validate_simulation_session_result(result), result)
        self.assertIsInstance(artifact_id, str)
        self.assertEqual(normalize_artifact_id(artifact_id), artifact_id)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["solver_run_record_ids"])
        self.assertTrue(result["evidence_artifact_ids"])
        self.assertTrue(result["journal_artifact_id"])

        run = make_solver_run_record(
            plan_id=self.plan["plan_id"],
            sequence=1,
            run_id=job_id,
            package_artifact_id=self.plan["base_package"]["artifact_id"],
            package_revision=self.plan["base_package"]["revision"],
            numerical_profile_revision=self.plan["recovery_profile_revision"],
            action="initial",
            status="completed",
            exit_code=0,
            artifact_ids=[artifact_id],
        )
        self.assertEqual(validate_solver_run_record(run), run)
        self.assertIn(run["record_id"], result["solver_run_record_ids"])

    def test_resume_is_idempotent_by_submit_count(self) -> None:
        ref = "session:resume"
        self._start(ref)
        self.assertEqual(self.queue.submit_count, 1)
        for _ in range(3):
            self.worker.resume_session(self.plan, self.allocation, ref)
        self.assertEqual(self.queue.submit_count, 1)

    def test_completed_history_fallback_when_active_job_disappears(self) -> None:
        ref = "session:history-fallback"
        job_id = self._start(ref)
        self.queue.complete(job_id)
        self.assertIsNone(self.queue.query_active(job_id))
        self.assertIn(job_id, self.queue.history)
        self.assertEqual(self.worker.observe_session(ref), "completed")

    def test_missing_active_and_history_is_indeterminate(self) -> None:
        ref = "session:expired"
        job_id = self._start(ref)
        self.queue.complete(job_id)
        self.queue.expire_history(job_id)
        self.assertIsNone(self.queue.query_active(job_id))
        self.assertIsNone(self.queue.query_history(job_id))
        self.assertEqual(self.worker.observe_session(ref), "indeterminate")

    def test_query_failure_is_unreachable(self) -> None:
        ref = "session:unreachable"
        self._start(ref)
        self.queue.fail_queries = True
        self.assertEqual(self.worker.observe_session(ref), "unreachable")

    def test_terminate_existing_job_verifies_terminal_state(self) -> None:
        ref = "session:terminate"
        job_id = self._start(ref)
        self.assertEqual(self.worker.terminate_session(ref), "terminated")
        # These assertions prove termination was observed after cancellation, rather
        # than inferred solely from cancel() returning successfully.
        self.assertIsNone(self.queue.query_active(job_id))
        terminal = self.queue.history.get(job_id)
        self.assertIsNotNone(terminal)
        self.assertEqual(terminal.state, "CANCELLED")

    def test_terminate_without_record_is_absent(self) -> None:
        self.assertEqual(self.worker.terminate_session("session:unknown"), "absent")


if __name__ == "__main__":
    unittest.main()
