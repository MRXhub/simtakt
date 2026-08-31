#!/usr/bin/env python3
"""Contracts for adaptive compute feedback and its scheduling boundary."""

from __future__ import annotations

import math
import unittest

from control_plane.evaluation.compute_profile import (
    MAX_AGGREGATE_COUNT,
    MAX_PROFILE_BUCKETS,
    ComputeProfileError,
    TaskShapeStats,
    estimate_shape,
    make_capacity_profile_snapshot,
    make_feedback_observation,
    make_task_class,
    make_task_class_key,
    make_task_override,
    validate_user_class_key,
    welford_stddev,
    welford_update,
)
from control_plane.evaluation.scheduling import (
    SchedulingError,
    schedule,
    scheduling_decision_plain,
    validate_scheduling_decision,
)
from control_plane.data.sqlite_evaluation_repository import RepositoryError
from tests.test_scheduling import (
    DECISION_TIME,
    REVISION,
    SCHEDULING_POLICY,
    choice,
    ranked_candidate,
    snapshot,
)


PROVENANCE = {
    "artifact_id": "configuration.project-scheduling-policy.fixture",
    "revision": "sha256:" + "c" * 64,
    "project_state_revision": "sha256:" + "d" * 64,
}


def task_class(revision: str = REVISION) -> dict:
    return make_task_class(
        simulation_definition_artifact_id="simulation-definition.fixture",
        simulation_definition_revision=revision,
        numerical_profile="newton-v2",
        recovery_profile_revision="sha256:" + "e" * 64,
    )


def learned_shape(class_: dict, processors: int, walls: tuple[float, ...]) -> dict:
    stats = TaskShapeStats(
        task_class_key=class_["key"], target_id="simulation.remote-primary",
        profile_revision=class_["boundary"]["simulation_definition"]["revision"],
        processors=processors,
    )
    # Frozen contract: measured completion-wall requires five terminal samples.
    # Keep fixture call sites concise while making the threshold explicit.
    samples = tuple(walls) * ((5 + len(walls) - 1) // len(walls))
    for wall in samples[:5]:
        stats.observe(make_feedback_observation(success=True, wall_seconds=wall))
    return stats.shape()


def adaptive_candidate(attempt_id: str, class_: dict, *choices: tuple[dict, dict]) -> dict:
    options, profiles = zip(*choices)
    from control_plane.evaluation.execution_options import make_execution_option_set, make_performance_profile_snapshot
    return {
        "attempt_id": attempt_id,
        "evaluation_id": "evaluation:" + attempt_id.removeprefix("attempt:"),
        "priority": "normal",
        "queued_since": "2026-08-03T11:30:00+00:00",
        "task_class": class_,
        "execution_option_set": make_execution_option_set(list(options)),
        "performance_profile_snapshot": make_performance_profile_snapshot(
            policy_revision=REVISION, profiles=list(profiles)
        ),
    }


def adaptive_schedule(candidate: dict, shapes: list[dict], *, overrides=(), processors=4) -> dict:
    return schedule(
        [candidate], [], snapshot(processors=processors),
        scheduling_policy=SCHEDULING_POLICY, decision_time=DECISION_TIME,
        capacity_profile_snapshot=make_capacity_profile_snapshot(shapes),
        scheduling_policy_provenance=PROVENANCE, overrides=overrides,
    )


class ComputeProfileContractsTests(unittest.TestCase):
    def test_task_class_key_is_revision_aware_and_payload_free(self) -> None:
        first = make_task_class_key(
            simulation_definition_artifact_id="definition.fixture",
            simulation_definition_revision=REVISION, numerical_profile="n1",
            recovery_profile_revision="sha256:" + "b" * 64, user_class_key="family.a",
        )
        self.assertEqual(first, make_task_class_key(
            simulation_definition_artifact_id="definition.fixture", simulation_definition_revision=REVISION,
            numerical_profile="n1", recovery_profile_revision="sha256:" + "b" * 64, user_class_key="family.a"))
        self.assertNotEqual(first, make_task_class_key(
            simulation_definition_artifact_id="definition.fixture", simulation_definition_revision="sha256:" + "f" * 64,
            numerical_profile="n1", recovery_profile_revision="sha256:" + "b" * 64, user_class_key="family.a"))
        self.assertNotIn("parameters", first)
        with self.assertRaises(ComputeProfileError):
            validate_user_class_key("X" * 65)

    def test_welford_bounds_and_failed_feedback_wall_semantics(self) -> None:
        mean = m2 = None
        count = 0
        for sample in (10, 20, 30):
            mean, m2 = welford_update(mean, m2, count, sample)
            count += 1
        self.assertEqual((count, mean, m2), (3, 20.0, 200.0))
        self.assertAlmostEqual(welford_stddev(m2, count), math.sqrt(200 / 3))
        with self.assertRaises(ComputeProfileError):
            welford_update(1.0, 0.0, MAX_AGGREGATE_COUNT, 2.0)
        with self.assertRaises(ComputeProfileError):
            make_feedback_observation(success=True, wall_seconds=0)
        with self.assertRaises(ComputeProfileError):
            make_feedback_observation(success=True, wall_seconds=9e99)
        stats = TaskShapeStats(task_class_key=task_class()["key"], target_id="t", profile_revision=REVISION, processors=1)
        stats.observe(make_feedback_observation(success=False))
        self.assertEqual((stats.failure_count, stats.wall_samples, stats.wall_mean), (1, 0, None))

    def test_snapshot_identity_bound_duplicate_and_override_range(self) -> None:
        class_ = task_class()
        shape = learned_shape(class_, 1, (10, 11, 12))
        with self.assertRaises(ComputeProfileError):
            make_capacity_profile_snapshot([shape, shape])
        with self.assertRaises(ComputeProfileError):
            make_task_override(task_class_key=class_["key"], latency_bias=1.01)
        # Distinct processor identities exercise the strict snapshot bound.
        shapes = [learned_shape(class_, index + 1, (1, 2, 3)) for index in range(MAX_PROFILE_BUCKETS + 1)]
        with self.assertRaises(ComputeProfileError):
            make_capacity_profile_snapshot(shapes)


class AdaptiveSchedulingContractsTests(unittest.TestCase):
    def test_measured_fast_p4_wins_low_pressure_but_p1_wins_full_pressure(self) -> None:
        class_ = task_class()
        p1, p4 = choice("p1", 1, 100), choice("p4", 4, 60)
        shapes = [
            learned_shape(class_, 1, (100, 100, 100)),
            learned_shape(class_, 4, (60, 60, 60)),
        ]
        low = adaptive_schedule(
            adaptive_candidate("attempt:low", class_, p1, p4), shapes
        )
        self.assertEqual(low["allocation"]["processors"], 4)
        crowded = schedule(
            [
                adaptive_candidate(f"attempt:crowded-{index}", class_, p1, p4)
                for index in range(4)
            ],
            [],
            snapshot(),
            scheduling_policy=SCHEDULING_POLICY,
            decision_time=DECISION_TIME,
            capacity_profile_snapshot=make_capacity_profile_snapshot(shapes),
            scheduling_policy_provenance=PROVENANCE,
        )
        self.assertEqual(crowded["allocation"]["processors"], 1)

    def test_candidate_ranking_reuses_selected_learned_score_record(self) -> None:
        first_class, second_class = task_class(), task_class("sha256:" + "f" * 64)
        first = adaptive_candidate("attempt:first", first_class, choice("first", 1, 10))
        second = adaptive_candidate("attempt:second", second_class, choice("second", 1, 200))
        decision = schedule(
            [first, second], [], snapshot(), scheduling_policy=SCHEDULING_POLICY,
            decision_time=DECISION_TIME,
            capacity_profile_snapshot=make_capacity_profile_snapshot([
                learned_shape(first_class, 1, (100, 100, 100)),
                learned_shape(second_class, 1, (10, 10, 10)),
            ]),
            scheduling_policy_provenance=PROVENANCE,
        )
        self.assertEqual(decision["selected_attempt_id"], "attempt:second")
        selected = next(item for item in decision["capacity_analysis"]["candidates"] if item["attempt_id"] == "attempt:second")
        self.assertEqual(selected["ranking_choice_key"], selected["selected_score_record"]["choice_key"])

    def test_evidence_bootstrap_scores_wall_not_core_seconds(self) -> None:
        class_ = task_class()
        candidate = adaptive_candidate(
            "attempt:bootstrap", class_, choice("p1", 1, 100), choice("p4", 4, 30)
        )
        decision = adaptive_schedule(candidate, [])
        self.assertEqual(decision["allocation"]["processors"], 4)

    def test_estimate_shape_keeps_zero_success_after_three_failures(self) -> None:
        class_ = task_class()
        stats = TaskShapeStats(task_class_key=class_["key"], target_id="simulation.remote-primary", profile_revision=REVISION, processors=1)
        for _ in range(3):
            stats.observe(make_feedback_observation(success=False))
        estimate = estimate_shape(choice("p1", 1, 100)[1], stats.shape())
        self.assertEqual(estimate["success_estimate"], 0.0)

    def test_scheduler_ranks_three_failures_after_positive_success(self) -> None:
        class_ = task_class()
        failures = TaskShapeStats(
            task_class_key=class_["key"], target_id="simulation.remote-primary",
            profile_revision=REVISION, processors=1,
        )
        for _ in range(3):
            failures.observe(make_feedback_observation(success=False))
        positive_class = task_class("sha256:" + "f" * 64)
        successful = learned_shape(positive_class, 1, (200, 200, 200))
        failed = adaptive_candidate("attempt:failed", class_, choice("failed", 1, 1, duration_p90_seconds=1))
        positive = adaptive_candidate("attempt:positive", positive_class, choice("positive", 1, 200, duration_p90_seconds=200))

        decision = schedule(
            [failed, positive], [], snapshot(), scheduling_policy=SCHEDULING_POLICY,
            decision_time=DECISION_TIME,
            capacity_profile_snapshot=make_capacity_profile_snapshot(
                [failures.shape(), successful]
            ),
            scheduling_policy_provenance=PROVENANCE,
        )

        self.assertEqual(decision["selected_attempt_id"], "attempt:positive")

    def test_scheduler_uses_fallback_wall_and_partial_success_in_score(self) -> None:
        class_ = task_class()
        partial = TaskShapeStats(
            task_class_key=class_["key"], target_id="simulation.remote-primary",
            profile_revision=REVISION, processors=1,
        )
        partial.observe(make_feedback_observation(success=True, wall_seconds=10))
        partial.observe(make_feedback_observation(success=True, wall_seconds=20))
        partial.observe(make_feedback_observation(success=False))
        other_class = task_class("sha256:" + "f" * 64)
        partial_candidate = adaptive_candidate(
            "attempt:partial", class_, choice("partial", 1, 100, duration_p90_seconds=110)
        )
        full_candidate = adaptive_candidate(
            "attempt:full", other_class, choice("full", 1, 180, duration_p90_seconds=180)
        )

        decision = schedule(
            [partial_candidate, full_candidate], [], snapshot(),
            scheduling_policy=SCHEDULING_POLICY, decision_time=DECISION_TIME,
            capacity_profile_snapshot=make_capacity_profile_snapshot([partial.shape()]),
            scheduling_policy_provenance=PROVENANCE,
        )

        analysis = next(
            item for item in decision["capacity_analysis"]["candidates"]
            if item["attempt_id"] == "attempt:partial"
        )["selected_score_record"]
        self.assertEqual(decision["selected_attempt_id"], "attempt:partial")
        self.assertEqual(analysis["wall_estimate_seconds"], 110.0)
        self.assertEqual(analysis["success_estimate_ppm"], 666_667)
        self.assertEqual(analysis["fallback_reason"], "insufficient-capacity-samples")

    def test_pressure_counts_total_target_legal_shape_while_active_work_blocks_it(self) -> None:
        class_ = task_class()
        candidate = adaptive_candidate(
            "attempt:queued", class_, choice("queued", 4, 10, memory_bytes=9 * 1024**3)
        )
        active = {
            "attempt_id": "attempt:active",
            "target_id": "simulation.remote-primary",
            "processors": 3,
            "memory_bytes": 2 * 1024**3,
            "resource_key": "/remote/active",
        }
        resources = snapshot(processors=1, observed=("/remote/active",))
        decision = schedule(
            [candidate], [active], resources, scheduling_policy=SCHEDULING_POLICY,
            decision_time=DECISION_TIME,
            capacity_profile_snapshot=make_capacity_profile_snapshot([]),
            scheduling_policy_provenance=PROVENANCE,
        )

        self.assertEqual(decision["action"], "wait")
        self.assertEqual(decision["capacity_analysis"]["queue_pressure"], 1.0)
        self.assertEqual(
            decision["capacity_analysis"]["pressure_basis"],
            "target-total-envelope-minimum-demand",
        )

    def test_estimate_shape_keeps_success_rate_when_wall_falls_back(self) -> None:
        class_ = task_class()
        stats = TaskShapeStats(task_class_key=class_["key"], target_id="simulation.remote-primary", profile_revision=REVISION, processors=1)
        stats.observe(make_feedback_observation(success=True, wall_seconds=10))
        stats.observe(make_feedback_observation(success=True, wall_seconds=20))
        stats.observe(make_feedback_observation(success=False))
        estimate = estimate_shape(choice("p1", 1, 100)[1], stats.shape())
        self.assertEqual(estimate["estimate_seconds"], 110.0)
        self.assertEqual(estimate["success_estimate"], 2 / 3)

    def test_learned_shape_uses_task_class_and_definition_revision(self) -> None:
        class_ = task_class()
        candidate = adaptive_candidate("attempt:adaptive", class_, choice("p1", 1, 100), choice("p4", 4, 40))
        evidence = schedule([candidate], [], snapshot(), scheduling_policy=SCHEDULING_POLICY, decision_time=DECISION_TIME)
        learned = adaptive_schedule(candidate, [learned_shape(class_, 1, (100, 100, 100)), learned_shape(class_, 4, (1, 1, 1))])
        self.assertEqual(evidence["allocation"]["processors"], 4)
        self.assertEqual(learned["allocation"]["processors"], 4)
        analysis = learned["capacity_analysis"]["candidates"][0]
        self.assertEqual(analysis["task_class"]["key"], class_["key"])
        self.assertEqual(analysis["profile_revision"], REVISION)

    def test_adaptive_requires_ranked_candidate_and_policy_provenance(self) -> None:
        class_ = task_class()
        ranked = adaptive_candidate("attempt:adaptive", class_, choice("p1", 1, 100))
        profiles = make_capacity_profile_snapshot([learned_shape(class_, 1, (10, 11, 12))])
        with self.assertRaisesRegex(SchedulingError, "provenance"):
            schedule([ranked], [], snapshot(), scheduling_policy=SCHEDULING_POLICY, decision_time=DECISION_TIME, capacity_profile_snapshot=profiles)
        unranked = {key: value for key, value in ranked.items() if key not in {"evaluation_id", "priority", "queued_since"}}
        with self.assertRaisesRegex(SchedulingError, "ranked"):
            schedule([unranked], [], snapshot(), capacity_profile_snapshot=profiles, scheduling_policy_provenance=PROVENANCE)

    def test_provenance_round_trips_and_overrides_cannot_bypass_hard_filters(self) -> None:
        class_ = task_class()
        candidate = adaptive_candidate("attempt:adaptive", class_, choice("p1", 1, 100), choice("p4", 4, 10, memory_bytes=9 * 1024**3))
        override = make_task_override(task_class_key=class_["key"], latency_bias=1)
        decision = adaptive_schedule(candidate, [learned_shape(class_, 1, (100, 100, 100)), learned_shape(class_, 4, (1, 1, 1))], overrides=[override])
        self.assertEqual(decision["allocation"]["processors"], 1)
        self.assertEqual(decision["scheduling_policy_provenance"], PROVENANCE)
        # Validation returns the canonical plain replay; the scheduler result
        # itself is recursively immutable.
        self.assertEqual(validate_scheduling_decision(decision), scheduling_decision_plain(decision))
        with self.assertRaisesRegex(SchedulingError, "unique"):
            adaptive_schedule(candidate, [learned_shape(class_, 1, (1, 2, 3))], overrides=[override, override])


class FeedbackRepositoryContractsTests(unittest.TestCase):
    """Use the prepared-dispatch fixture rather than bypassing Attempt invariants."""

    def setUp(self) -> None:
        from tests.test_prepared_execution_dispatcher import PreparedExecutionDispatcherTests
        self.fixture = PreparedExecutionDispatcherTests("run")
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def test_terminal_feedback_is_durable_idempotent_and_failed_wallless_is_not_sampled(self) -> None:
        prepared = self.fixture.prepare()
        repository = self.fixture.middleware._repository
        observation = make_feedback_observation(success=False)
        with self.assertRaises(RepositoryError):
            repository.record_attempt_feedback(prepared["attempt_id"], **observation)
        preparation, option, plan, allocation = self.fixture.claim_materials(prepared)
        self.fixture.middleware.claim_prepared_execution(
            prepared["attempt_id"], "dispatcher:fixture", 120,
            preparation_id=preparation["preparation_id"], selected_option_id=option["option_id"],
            session_plan=plan, allocation=allocation,
        )
        self.fixture.middleware.confirm_attempt_start(
            prepared["attempt_id"], "dispatcher:fixture"
        )
        self.fixture.middleware.begin_collection(
            prepared["attempt_id"], "dispatcher:fixture"
        )
        repository.fail_attempt(prepared["attempt_id"], "dispatcher:fixture", "fixture-failure", feedback=observation)
        replay = repository.record_attempt_feedback(prepared["attempt_id"], **observation)
        self.assertEqual(
            {key: replay[key] for key in observation}, observation
        )
        with self.assertRaises(RepositoryError):
            repository.record_attempt_feedback(prepared["attempt_id"], **make_feedback_observation(success=False, cpu_seconds=1))
        # Derive the identity from the prepared option rather than mixing this
        # module's revision fixture with the dispatcher fixture's revision.
        definition = option["simulation_definition"]
        identity = {
            "simulation_definition_artifact_id": definition["artifact_id"],
            "simulation_definition_revision": definition["revision"],
            "numerical_profile": preparation["numerical_profile"],
            "recovery_profile_revision": preparation["recovery_profile_revision"],
            "target_id": option["target_id"],
        }
        shapes = self.fixture.middleware.capacity_profile_snapshot([identity])["shapes"]
        self.assertEqual(len(shapes), 1)
        self.assertEqual((shapes[0]["failure_count"], shapes[0]["successful_wall_samples"], shapes[0]["successful_wall_mean_seconds"]), (1, 0, None))


if __name__ == "__main__":
    unittest.main()
