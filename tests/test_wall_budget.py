#!/usr/bin/env python3
"""Wall-budget resolution and persistence contract tests.

Two groups live here:
  * pure unit tests of ``resolve_wall_budget`` against a scripted fake
    repository (source degradation, the p95/1.2x-max guard, the declared
    floor, and kill-rate widening), and
  * real-SQLite integration tests proving the immutable budget is persisted
    when an Attempt starts, drives the wall-proof kill threshold, and falls
    back to ``1.7 x max_wall`` for legacy rows that predate ``wall_budget_json``.
"""

from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from control_plane.core.evaluation_contracts import (
    make_candidate,
    make_evaluation_request,
    make_problem_definition,
)
from control_plane.data.sqlite_evaluation_repository import (
    SCHEMA_VERSION,
    RepositoryError,
    SQLiteEvaluationRepository,
)
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
from control_plane.evaluation.service import EvaluationMiddleware
from control_plane.evaluation.wall_budget import resolve_wall_budget


REVISION = "sha256:" + "1" * 64
FIDELITY = "full-tcad"
TARGET = "simulation.remote-primary"
WORKER = "worker:fixture"
PERFORMANCE_CLASS_ID = "performance-class:sha256:" + "2" * 64


class _FakeSampleRepository:
    """Scripted stand-in exposing only the two wall-budget repository queries."""

    def __init__(self, samples=None, kills=None):
        self._samples = samples if samples is not None else {}
        self._kills_map = kills if isinstance(kills, dict) else {}
        self._kills_default = int(kills) if isinstance(kills, (int, float)) else 0

    def list_completed_wall_samples(
        self, problem_revision, fidelity=None, target_id=None, limit=200
    ):
        values = self._samples.get((fidelity, target_id), [])
        return [
            {"measured_wall_seconds": float(value)} for value in values[:limit]
        ]

    def count_wall_budget_kills(self, problem_revision, fidelity=None, target_id=None):
        if isinstance(self._kills_map, dict):
            return self._kills_map.get((fidelity, target_id), self._kills_default)
        return self._kills_default


def _resolve(repo, *, declared=60, fidelity=FIDELITY, target=TARGET):
    return resolve_wall_budget(
        repo,
        problem_revision=REVISION,
        fidelity=fidelity,
        target_id=target,
        declared_max_wall_seconds=declared,
        policy={},
    )


class ResolveWallBudgetUnitTests(unittest.TestCase):
    def test_no_wall_samples_resolves_to_declared(self) -> None:
        result = _resolve(_FakeSampleRepository())
        self.assertEqual(result["source"], "declared")
        self.assertEqual(result["sample_count"], 0)
        self.assertFalse(result["widened"])
        self.assertEqual(result["budget_seconds"], 60)
        # kill_at = 1.7 x budget, stall = 0.25 x budget.
        self.assertEqual(result["kill_at_seconds"], math.ceil(1.7 * 60))
        self.assertEqual(result["stall_seconds"], math.ceil(0.25 * 60))

    def test_declared_value_acts_as_floor_when_samples_are_small(self) -> None:
        # Twenty tiny samples: p95 and 1.2 x max are far below the declared 1000.
        repo = _FakeSampleRepository(
            samples={(FIDELITY, TARGET): [5.0] * 20}
        )
        result = _resolve(repo, declared=1000)
        self.assertEqual(result["source"], "learned:problem+fidelity+target")
        self.assertEqual(result["sample_count"], 20)
        self.assertEqual(result["budget_seconds"], 1000)
        self.assertEqual(result["kill_at_seconds"], math.ceil(1.7 * 1000))
        self.assertEqual(result["stall_seconds"], math.ceil(0.25 * 1000))

    def test_budget_takes_larger_of_p95_and_1_2x_max(self) -> None:
        # Spread 100..119 gives p95 ~= 118 but 1.2 x max = 142.8, which wins.
        samples = [float(100 + i) for i in range(20)]
        ordered = sorted(samples)
        rank = max(1, math.ceil(0.95 * len(ordered))) - 1
        p95 = ordered[rank]
        one_point_two_max = 1.2 * max(ordered)
        self.assertLess(p95, one_point_two_max)
        repo = _FakeSampleRepository(samples={(FIDELITY, TARGET): samples})
        result = _resolve(repo, declared=10)
        self.assertEqual(
            result["budget_seconds"], math.ceil(max(p95, one_point_two_max, 10))
        )
        # The resolved budget is strictly above the raw p95: the 1.2x-max guard binds.
        self.assertGreaterEqual(result["budget_seconds"], math.ceil(one_point_two_max))

    def test_budget_is_not_below_one_point_two_times_the_worst_sample(self) -> None:
        # Uniform samples: p95 equals the max here, but the safety multiplier
        # still raises the budget above the raw sample magnitude.
        repo = _FakeSampleRepository(
            samples={(FIDELITY, TARGET): [200.0] * 20}
        )
        result = _resolve(repo, declared=1)
        self.assertEqual(result["sample_count"], 20)
        self.assertGreaterEqual(result["budget_seconds"], math.ceil(1.2 * 200))

    def test_sparse_target_samples_degrade_to_problem_fidelity(self) -> None:
        # target-specific bucket holds 4 (< min_budget_samples) -> drop target.
        repo = _FakeSampleRepository(
            samples={
                (FIDELITY, TARGET): [10.0, 11.0, 12.0, 13.0],
                (FIDELITY, None): [100.0] * 5,
            }
        )
        result = _resolve(repo)
        self.assertEqual(result["source"], "learned:problem+fidelity")
        self.assertEqual(result["sample_count"], 5)

    def test_sparse_target_and_fidelity_degrade_to_problem_level(self) -> None:
        repo = _FakeSampleRepository(
            samples={
                (FIDELITY, TARGET): [1.0, 2.0, 3.0],
                (FIDELITY, None): [4.0, 5.0, 6.0, 7.0],
                (None, None): [500.0] * 5,
            }
        )
        result = _resolve(repo)
        self.assertEqual(result["source"], "learned:problem")
        self.assertEqual(result["sample_count"], 5)

    def test_sufficient_target_samples_use_most_specific_source(self) -> None:
        repo = _FakeSampleRepository(
            samples={
                (FIDELITY, TARGET): [10.0] * 5,
                (FIDELITY, None): [20.0] * 5,
                (None, None): [30.0] * 5,
            }
        )
        result = _resolve(repo)
        self.assertEqual(result["source"], "learned:problem+fidelity+target")
        self.assertEqual(result["sample_count"], 5)

    def test_kill_rate_above_threshold_widens_budget_by_one_point_five(self) -> None:
        # base = ceil(1.2 * 100) = 120; 3 kills / (3 + 20) = 0.130 > 0.10.
        repo = _FakeSampleRepository(
            samples={(FIDELITY, TARGET): [100.0] * 20},
            kills={(FIDELITY, TARGET): 3},
        )
        result = _resolve(repo, declared=10)
        self.assertTrue(result["widened"])
        self.assertEqual(result["budget_seconds"], math.ceil(1.5 * 120))
        self.assertEqual(result["kill_at_seconds"], math.ceil(1.7 * 1.5 * 120))
        self.assertEqual(result["stall_seconds"], math.ceil(0.25 * 1.5 * 120))

    def test_kill_rate_at_or_below_threshold_does_not_widen(self) -> None:
        # 1 kill / (1 + 20) = 0.0476 <= 0.10 -> unchanged.
        repo = _FakeSampleRepository(
            samples={(FIDELITY, TARGET): [100.0] * 20},
            kills={(FIDELITY, TARGET): 1},
        )
        result = _resolve(repo, declared=10)
        self.assertFalse(result["widened"])
        self.assertEqual(result["budget_seconds"], 120)

    def test_declared_without_kills_keeps_default_multipliers(self) -> None:
        repo = _FakeSampleRepository(samples={(FIDELITY, TARGET): [100.0] * 5})
        result = _resolve(repo, declared=80)
        self.assertFalse(result["widened"])
        # 1.2 x max (120) exceeds declared 80, so learning dominates.
        self.assertEqual(result["budget_seconds"], 120)
        self.assertEqual(result["kill_at_seconds"], math.ceil(1.7 * 120))
        self.assertEqual(result["stall_seconds"], math.ceil(0.25 * 120))


class WallBudgetRepositoryIntegrationTests(unittest.TestCase):
    """Real-SQLite checks of budget persistence, the wall-proof clock, and the
    legacy 1.7 x max_wall fallback."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "control.sqlite3"
        self.repository = SQLiteEvaluationRepository(self.database)
        from tests.shared_fixtures import register_fixture_schema
        schema_revision = register_fixture_schema(
            self.repository, problem_hint="wall-budget"
        )
        self.problem = make_problem_definition(
            problem_id="wall-budget-fixture",
            parameter_schema_revision=schema_revision,
            constraint_revision=REVISION,
            simulation_capabilities=["full-tcad"],
            metric_schema_revision=REVISION,
        )
        self.repository.register_problem(self.problem)
        self.submission_count = 0
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
        evaluation = self.repository.submit_evaluation(candidate, request)
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
            command_timeout_seconds=600,
            max_solver_runs=1,
            max_wall_seconds=900,
            execution_option_set=make_execution_option_set([option]),
            performance_profile_snapshot=make_performance_profile_snapshot(
                policy_revision=REVISION,
                profiles=[profile],
            ),
        )

    def _prepare(self) -> dict:
        submission = self.submit()
        return self.repository.create_prepared_attempt(
            self._preparation(submission)
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
        leased = self.repository.claim_prepared_execution(
            attempt["attempt_id"], WORKER, 300,
            preparation_id=preparation["preparation_id"],
            selected_option_id=option["option_id"],
            session_plan=plan,
            allocation=allocation,
            now=self.clock,
        )
        self.assertIsNotNone(leased)
        return leased, preparation, option

    def _start_running(self, attempt: dict) -> dict:
        self._lease(attempt)
        self.repository.confirm_attempt_start(
            attempt["attempt_id"], WORKER, now=self.clock
        )
        return attempt

    def _reconcile(self, attempt: dict) -> None:
        self._start_running(attempt)
        self.repository.mark_attempt_reconciling(
            attempt["attempt_id"], WORKER, ["artifact:recon"],
            reason="fixture", now=self.clock,
        )

    def _iso(self, moment: datetime) -> str:
        return moment.isoformat()

    def _set_wall_budget_json(self, attempt_id: str, value: dict | None) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE attempts SET wall_budget_json = ? WHERE attempt_id = ?",
                (None if value is None else json.dumps(value), attempt_id),
            )
            connection.commit()

    def test_attempt_start_persists_wall_budget_json_with_full_fields(self) -> None:
        submission = self.submit()
        planned = self.repository.schedule_attempt(
            evaluation_id=submission["evaluation"]["evaluation_id"],
            simulation_adapter="fixture-adapter",
            numerical_profile="fixture-profile",
        )
        leased = self.repository.lease_next_attempt(
            "worker:1", 600, now=self.clock
        )
        self.assertIsNotNone(leased)
        resolved = {
            "budget_seconds": 120,
            "kill_at_seconds": 204,
            "stall_seconds": 30,
            "source": "declared",
            "sample_count": 0,
            "widened": False,
        }
        self.repository.start_attempt(
            leased["attempt_id"], "worker:1",
            now=self.clock + timedelta(seconds=1),
            wall_budget=resolved,
        )
        stored = self.repository.get_attempt(leased["attempt_id"])["wall_budget"]
        self.assertEqual(stored, resolved)
        self.assertEqual(
            set(stored),
            {"budget_seconds", "kill_at_seconds", "stall_seconds",
             "source", "sample_count", "widened"},
        )

        # RED TEST: records an integration bug. The immutable budget's
        # kill_at_seconds is persisted at Attempt start, and the wall-proof
        # contract says an Attempt must only be released once that persisted
        # threshold elapses.  In practice EvaluationMiddleware reads
        # candidate["wall_budget"], but repository.list_reconciling_attempts_for_wall_proof
        # never populates that key, so the persisted budget is ignored and the
        # attempt falls back to 1.7 x plan max_wall instead.


    def test_wall_proof_ignored_before_kill_at_and_releases_after(self) -> None:
        attempt = self._prepare()
        self._reconcile(attempt)
        # Persisted budget carries an explicit 100 s kill threshold.
        self._set_wall_budget_json(attempt["attempt_id"], {"kill_at_seconds": 100})
        middleware = EvaluationMiddleware(self.repository)
        self.assertEqual(
            middleware.auto_release_wall_budget(
                now=self.clock + timedelta(seconds=99)
            ),
            [],
        )
        self.assertEqual(
            self.repository.get_attempt(attempt["attempt_id"])["status"],
            "reconciling",
        )
        released = middleware.auto_release_wall_budget(
            now=self.clock + timedelta(seconds=101)
        )
        self.assertEqual(len(released), 1)
        self.assertEqual(released[0]["status"], "released")
        self.assertEqual(released[0]["proof_seconds"], 100)
        self.assertEqual(
            self.repository.get_attempt(attempt["attempt_id"])["status"], "lost"
        )

    def test_legacy_row_without_wall_budget_json_falls_back_to_1_7x_max_wall(self) -> None:
        attempt = self._prepare()
        self._reconcile(attempt)
        # Strip the persisted budget to emulate a pre-v16 row.
        self._set_wall_budget_json(attempt["attempt_id"], None)
        middleware = EvaluationMiddleware(self.repository)
        self.assertEqual(
            middleware.auto_release_wall_budget(
                now=self.clock + timedelta(seconds=1529)
            ),
            [],
        )
        released = middleware.auto_release_wall_budget(
            now=self.clock + timedelta(seconds=1531)
        )
        self.assertEqual(len(released), 1)
        # max_wall is 900 in the fixture -> 1.7 x 900 = 1530.
        self.assertEqual(released[0]["proof_seconds"], 1530)

    def test_wall_budget_killed_attempt_is_excluded_from_samples_and_counted_as_kill(
        self,
    ) -> None:
        # One successful completed attempt contributes a wall sample.
        completed = self._prepare()
        self._lease(completed)
        self.clock += timedelta(seconds=10)
        self.repository.confirm_attempt_start(
            completed["attempt_id"], WORKER, now=self.clock
        )
        self.repository.begin_collection(completed["attempt_id"], WORKER, now=self.clock)
        self.repository.complete_attempt(
            completed["attempt_id"], WORKER, ["artifact.completed"],
            _validated_session_result=True, now=self.clock,
            feedback=make_feedback_observation(success=True, wall_seconds=777.0),
        )
        # A second attempt is killed by the wall proof.
        killed = self._prepare()
        self.clock += timedelta(seconds=20)
        self._reconcile(killed)
        released = EvaluationMiddleware(self.repository).auto_release_wall_budget(
            now=self.clock + timedelta(seconds=1600)
        )
        self.assertTrue(any(r["attempt_id"] == killed["attempt_id"] for r in released))

        samples = self.repository.list_completed_wall_samples(
            self.problem["revision"], "full-tcad"
        )
        sample_ids = {row["attempt_id"] for row in samples}
        self.assertIn(completed["attempt_id"], sample_ids)
        self.assertNotIn(killed["attempt_id"], sample_ids)
        measured = {
            row["attempt_id"]: row["measured_wall_seconds"] for row in samples
        }
        self.assertEqual(measured[completed["attempt_id"]], 777.0)
        self.assertEqual(
            self.repository.count_wall_budget_kills(
                self.problem["revision"], "full-tcad"
            ),
            1,
        )

    def _complete_wall_sample(self, wall_seconds: float) -> dict:
        """Drive one successful completed Attempt through the real feedback path."""
        attempt = self._prepare()
        self._lease(attempt)
        self.clock += timedelta(seconds=1)
        self.repository.confirm_attempt_start(
            attempt["attempt_id"], WORKER, now=self.clock
        )
        self.repository.begin_collection(
            attempt["attempt_id"], WORKER, now=self.clock
        )
        self.repository.complete_attempt(
            attempt["attempt_id"], WORKER, ["artifact.completed"],
            _validated_session_result=True, now=self.clock,
            feedback=make_feedback_observation(
                success=True, wall_seconds=wall_seconds
            ),
        )
        return attempt

    def test_confirm_start_resolves_learned_budget_from_real_feedback_samples(
        self,
    ) -> None:
        """Launch confirmation learns a wall budget once >=5 real samples exist.

        Regression: resolve_wall_budget is wired into the production startup
        path.  After 5 successful completed samples are recorded through the
        real feedback path for the same problem+fidelity+target key, confirming
        a new Attempt persists a budget whose source starts with ``learned:``,
        whose ``kill_at_seconds`` equals ``ceil(1.7 * budget_seconds)``, and
        which is not the declared-source fallback.
        """
        # The fixture plan declares max_wall=900 -> declared fallback would be
        # kill_at = ceil(1.7 * 900) = 1530.  Samples far above 900 make the
        # learned budget (and therefore its kill_at) exceed that fallback.
        for _ in range(5):
            self._complete_wall_sample(5000.0)

        target = self._prepare()
        self._lease(target)
        self.clock += timedelta(seconds=1)
        self.repository.confirm_attempt_start(
            target["attempt_id"], WORKER, now=self.clock
        )
        budget = self.repository.get_attempt(target["attempt_id"])["wall_budget"]
        self.assertIsNotNone(budget)
        self.assertTrue(
            budget["source"].startswith("learned:"),
            f"expected a learned source, got {budget['source']!r}",
        )
        self.assertEqual(budget["sample_count"], 5)
        self.assertEqual(
            budget["kill_at_seconds"],
            math.ceil(1.7 * budget["budget_seconds"]),
        )
        self.assertGreater(budget["budget_seconds"], 900)
        self.assertNotEqual(
            budget["kill_at_seconds"], math.ceil(1.7 * 900)
        )


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
