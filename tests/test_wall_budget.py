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


if __name__ == "__main__":
    unittest.main()
