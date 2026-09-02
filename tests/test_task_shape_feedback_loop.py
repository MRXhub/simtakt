#!/usr/bin/env python3
"""Closed-loop tests for the task-shape feedback write end.

These prove that a terminal Attempt now feeds one feedback observation into the
task_shape_stats accumulator (through TaskShapeStats.observe), that the
capacity profile snapshot reads that data back, that the write is idempotent,
that failed/lost attempts count as unsuccessful without requiring wall_seconds,
that a feedback write failure never aborts task termination, and that distinct
task classes never bleed statistics.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from control_plane.core.evaluation_contracts import (
    make_candidate,
    make_evaluation_request,
    make_problem_definition,
)
from control_plane.data.sqlite_evaluation_repository import SQLiteEvaluationRepository
from control_plane.evaluation.compute_profile import make_feedback_observation
from control_plane.evaluation.execution_options import (
    make_execution_option,
    make_execution_option_set,
    make_execution_preparation,
    make_performance_profile,
    make_performance_profile_snapshot,
)
from control_plane.evaluation.execution_planning import materialize_session_plan
from control_plane.evaluation.scheduling import make_resource_allocation, schedule


REVISION = "sha256:" + "1" * 64
PERFORMANCE_CLASS_ID = "performance-class:sha256:" + "2" * 64
TARGET = "simulation.remote-primary"
OTHER_TARGET = "simulation.remote-secondary"
WORKER = "worker:fixture"


class TaskShapeFeedbackLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "control.sqlite3"
        self.repo = SQLiteEvaluationRepository(self.database)
        self.problem = make_problem_definition(
            problem_id="feedback-loop-fixture",
            parameter_schema_revision=REVISION,
            constraint_revision=REVISION,
            simulation_capabilities=["full-tcad"],
            metric_schema_revision=REVISION,
        )
        self.repo.register_problem(self.problem)
        self.submission_count = 0
        # created_at is stamped with the real wall clock; start our test clock a
        # fixed margin in the future so computed wall_seconds stay positive no
        # matter how long the full suite takes to reach this test.
        self.clock = datetime.now(timezone.utc) + timedelta(hours=1)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def submit(self) -> dict:
        self.submission_count += 1
        candidate = make_candidate(
            problem_id=self.problem["problem_id"],
            problem_revision=self.problem["revision"],
            parameters={"x": float(self.submission_count)},
        )
        request = make_evaluation_request(
            candidate_id=candidate["candidate_id"],
            fidelity="full-tcad",
            requested_outputs=["Eff"],
            evidence_profile="fixture-v1",
        )
        evaluation = self.repo.submit_evaluation(candidate, request)
        return {"candidate": candidate, "evaluation": evaluation}

    def _preparation(self, submission: dict, *, target_id: str = TARGET) -> dict:
        option = make_execution_option(
            simulation_definition_artifact_id="simulation-definition.fixture",
            simulation_definition_revision=REVISION,
            runnable_package_artifact_id="package.fixture-p1",
            runnable_package_revision=REVISION,
            target_id=target_id,
            processors=1,
            memory_bytes=4 * 1024**3,
            performance_class_id=PERFORMANCE_CLASS_ID,
        )
        profile = make_performance_profile(
            execution_option_id=option["option_id"],
            evidence_artifact_id="evidence.performance.fixture",
            evidence_revision=REVISION,
            sample_count=2,
            duration_p50_seconds=120,
            duration_p90_seconds=150,
            peak_rss_p90_bytes=2 * 1024**3,
            performance_class_id=PERFORMANCE_CLASS_ID,
        )
        return make_execution_preparation(
            evaluation_id=submission["evaluation"]["evaluation_id"],
            candidate_id=submission["candidate"]["candidate_id"],
            simulation_proxy="simulation-session-v1",
            numerical_profile="proxy-managed-v1",
            recovery_profile_revision=REVISION,
            task_id="fixture-task",
            authorization_id="authorization.fixture",
            authorization_revision=REVISION,
            command_timeout_seconds=600,
            max_solver_runs=1,
            max_wall_seconds=900,
            execution_option_set=make_execution_option_set([option]),
            performance_profile_snapshot=make_performance_profile_snapshot(
                policy_revision=REVISION,
                profiles=[profile],
            ),
        )

    def _prepare(self, *, target_id: str = TARGET) -> dict:
        submission = self.submit()
        return self.repo.create_prepared_attempt(
            self._preparation(submission, target_id=target_id)
        )

    def _lease(self, attempt: dict) -> tuple[dict, dict, dict]:
        preparation = attempt["execution_preparation"]
        target_id = preparation["execution_option_set"]["options"][0]["target_id"]
        resources = {
            "schema_version": 1,
            "snapshot_kind": "resource-snapshot",
            "snapshot_revision": REVISION,
            "target_id": target_id,
            "status": "ready",
            "available_processors": 1,
            "available_memory_bytes": 8 * 1024**3,
            "default_request_memory_bytes": 4 * 1024**3,
            "observed_allocation_keys": [],
            "reasons": [],
            "created_at": self.clock.isoformat(),
            "lock_held": True,
            "target_is_idle": True,
        }
        decision = schedule(
            [
                {
                    "attempt_id": attempt["attempt_id"],
                    "execution_option_set": preparation["execution_option_set"],
                    "performance_profile_snapshot": preparation[
                        "performance_profile_snapshot"
                    ],
                }
            ],
            [],
            resources,
        )
        option = decision["selected_execution_option"]
        plan = materialize_session_plan(
            attempt_id=attempt["attempt_id"],
            preparation=preparation,
            selected_option=option,
        )
        suffix = attempt["attempt_id"].split(":")[-1][:12]
        allocation = make_resource_allocation(
            decision,
            session_ref=f"session-{suffix}",
            run_id="20260803-000000-001",
            remote_workspace_root="/remote/test-workspace",
            decision_artifact_id="evidence.scheduling.fixture",
            decision_artifact_path="decision.json",
        )
        leased = self.repo.claim_prepared_execution(
            attempt["attempt_id"], WORKER, 300,
            preparation_id=preparation["preparation_id"],
            selected_option_id=option["option_id"],
            session_plan=plan, allocation=allocation, now=self.clock,
        )
        self.assertIsNotNone(leased)
        return leased, preparation, option

    def _complete(self, attempt: dict, *, feedback=None) -> tuple[dict, dict, dict]:
        _, preparation, option = self._lease(attempt)
        self.clock += timedelta(seconds=10)
        self.repo.confirm_attempt_start(attempt["attempt_id"], WORKER, now=self.clock)
        self.repo.begin_collection(attempt["attempt_id"], WORKER, now=self.clock)
        result = self.repo.complete_attempt(
            attempt["attempt_id"], WORKER, ["artifact.completed"],
            _validated_session_result=True, now=self.clock, feedback=feedback,
        )
        return result, preparation, option

    def _fail(self, attempt: dict, *, feedback=None) -> tuple[dict, dict, dict]:
        _, preparation, option = self._lease(attempt)
        self.clock += timedelta(seconds=10)
        self.repo.confirm_attempt_start(attempt["attempt_id"], WORKER, now=self.clock)
        self.repo.begin_collection(attempt["attempt_id"], WORKER, now=self.clock)
        result = self.repo.fail_attempt(
            attempt["attempt_id"], WORKER, "fixture-failure",
            ["artifact.failed"], now=self.clock, feedback=feedback,
        )
        return result, preparation, option

    def _identity(self, preparation: dict, option: dict) -> dict:
        definition = option["simulation_definition"]
        return {
            "simulation_definition_artifact_id": definition["artifact_id"],
            "simulation_definition_revision": definition["revision"],
            "numerical_profile": preparation["numerical_profile"],
            "recovery_profile_revision": preparation["recovery_profile_revision"],
            "target_id": option["target_id"],
        }

    def _snapshot(self, identity: dict) -> list[dict]:
        return self.repo.get_capacity_profile_snapshot([identity])["shapes"]

    def test_closed_loop_reads_back_five_successful_feedback_observations(self) -> None:
        # Closed loop: the termination path records feedback, and
        # get_capacity_profile_snapshot() must read that data back.
        probe = self._prepare()
        _, prep, opt = self._lease(probe)
        identity = self._identity(prep, opt)
        self.assertEqual(self._snapshot(identity), [])

        for _ in range(5):
            attempt = self._prepare()
            self._complete(attempt)

        shapes = self._snapshot(identity)
        self.assertEqual(len(shapes), 1)
        shape = shapes[0]
        self.assertEqual(shape["sample_count"], 5)
        self.assertEqual(shape["success_count"], 5)
        self.assertEqual(shape["failure_count"], 0)
        # Auto-feedback no longer guesses duration from queue/preparation elapsed time;
        # only explicitly supplied simulation wall durations are valid.
        self.assertEqual(shape["successful_wall_samples"], 0)
        self.assertIsNone(shape["successful_wall_mean_seconds"])

    def test_terminal_path_is_idempotent_across_reentry(self) -> None:
        attempt = self._prepare()
        _, prep, opt = self._complete(attempt)
        identity = self._identity(prep, opt)
        self.assertEqual(self._snapshot(identity)[0]["sample_count"], 1)

        # Re-running the same terminal transition must not re-record.
        self.repo.complete_attempt(
            attempt["attempt_id"], WORKER, ["artifact.completed"],
            _validated_session_result=True, now=self.clock,
        )
        # Re-entry through the middleware auto-feedback path must also no-op.
        self.repo._record_auto_feedback(attempt["attempt_id"], success=True)
        self.repo._record_auto_feedback(attempt["attempt_id"], success=True)
        self.assertEqual(self._snapshot(identity)[0]["sample_count"], 1)

    def test_failed_task_records_success_false_without_wall_seconds_error(self) -> None:
        attempt = self._prepare()
        failed, _, _ = self._fail(attempt)
        self.assertEqual(failed["status"], "failed")
        feedback = self.repo.get_attempt_feedback(attempt["attempt_id"])
        self.assertIsNotNone(feedback)
        self.assertIs(feedback["success"], False)
        # cpu/busy/rss are out of scope this batch and must be None, not required.
        self.assertIsNone(feedback["cpu_seconds"])
        self.assertIsNone(feedback["busy_seconds"])
        self.assertIsNone(feedback["rss_bytes"])

    def test_feedback_write_failure_never_aborts_termination(self) -> None:
        attempt = self._prepare()
        with mock.patch.object(
            self.repo, "_record_attempt_feedback_in_transaction",
            side_effect=RuntimeError("stats disk full"),
        ):
            failed, _, _ = self._fail(attempt)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self.repo.get_attempt(attempt["attempt_id"])["status"], "failed")

    def test_welford_mean_and_stddev_match_hand_calculation(self) -> None:
        samples = [10.0, 20.0, 30.0]
        identity = None
        for wall in samples:
            attempt = self._prepare()
            _, prep, opt = self._lease(attempt)
            identity = self._identity(prep, opt)
            self.clock += timedelta(seconds=10)
            self.repo.confirm_attempt_start(attempt["attempt_id"], WORKER, now=self.clock)
            self.repo.begin_collection(attempt["attempt_id"], WORKER, now=self.clock)
            self.repo.complete_attempt(
                attempt["attempt_id"], WORKER, ["artifact.completed"],
                _validated_session_result=True, now=self.clock,
                feedback=make_feedback_observation(success=True, wall_seconds=wall),
            )
        shape = self._snapshot(identity)[0]
        mean = sum(samples) / len(samples)
        variance = sum((s - mean) ** 2 for s in samples) / len(samples)
        self.assertAlmostEqual(shape["successful_wall_mean_seconds"], mean)
        self.assertAlmostEqual(shape["successful_wall_stddev_seconds"], variance ** 0.5)
        self.assertEqual(shape["successful_wall_samples"], 3)

    def test_mixed_explicit_wall_durations_exclude_missing_values(self) -> None:
        identity = None
        for wall in (10.0, 20.0, 30.0):
            attempt = self._prepare()
            _, prep, opt = self._complete(
                attempt,
                feedback=make_feedback_observation(success=True, wall_seconds=wall),
            )
            identity = self._identity(prep, opt)
        for _ in range(2):
            self._complete(self._prepare())

        shape = self._snapshot(identity)[0]
        self.assertEqual(shape["sample_count"], 5)
        self.assertEqual(shape["success_count"], 5)
        self.assertEqual(shape["successful_wall_samples"], 3)
        self.assertAlmostEqual(shape["successful_wall_mean_seconds"], 20.0)
        self.assertAlmostEqual(
            shape["successful_wall_stddev_seconds"], (200.0 / 3.0) ** 0.5
        )
    def test_distinct_task_classes_do_not_mix_statistics(self) -> None:
        for _ in range(3):
            attempt = self._prepare(target_id=TARGET)
            self._complete(attempt)
        for _ in range(2):
            attempt = self._prepare(target_id=OTHER_TARGET)
            self._complete(attempt)

        a = self._prepare(target_id=TARGET)
        b = self._prepare(target_id=OTHER_TARGET)
        _, prep1, opt1 = self._lease(a)
        _, prep2, opt2 = self._lease(b)
        identity1 = self._identity(prep1, opt1)
        identity2 = self._identity(prep2, opt2)
        self.assertNotEqual(identity1["target_id"], identity2["target_id"])

        shape1 = self._snapshot(identity1)[0]
        shape2 = self._snapshot(identity2)[0]
        self.assertEqual(shape1["target_id"], TARGET)
        self.assertEqual(shape2["target_id"], OTHER_TARGET)
        self.assertEqual(shape1["sample_count"], 3)
        self.assertEqual(shape2["sample_count"], 2)
        self.assertEqual(shape1["task_class_key"], shape2["task_class_key"])


if __name__ == "__main__":
    unittest.main()
