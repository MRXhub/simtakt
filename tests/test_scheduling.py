#!/usr/bin/env python3
"""Pure scheduling checks for the evaluation control plane."""

from __future__ import annotations

import unittest

from control_plane.evaluation.execution_options import (
    make_execution_option,
    make_execution_option_set,
    make_parallel_efficiency_calibration,
    make_performance_profile,
    make_performance_profile_snapshot,
)
from control_plane.evaluation.scheduling import (
    SchedulingError,
    make_resource_allocation,
    schedule,
    schedule_legacy_v1,
    scheduling_decision_plain,
    validate_scheduling_decision,
)


TARGET = "simulation.remote-primary"
OTHER_TARGET = "simulation.remote-secondary"
REVISION = "sha256:" + "a" * 64
PERFORMANCE_CLASS = "performance-class:sha256:" + "b" * 64
DECISION_TIME = "2026-08-03T12:00:00+00:00"
SCHEDULING_POLICY = {
    "priority_order": ["urgent", "normal", "low"],
    "default_priority": "normal",
    "aging_quantum_seconds": 3600,
}


def candidate(attempt_id: str, processors: int) -> dict:
    return {
        "attempt_id": attempt_id,
        "target_id": TARGET,
        "requested_processors": processors,
        "requested_memory_bytes": 4 * 1024**3,
    }


def snapshot(
    *, processors: int = 4, observed: tuple[str, ...] = (), target_is_idle: bool | None = None
) -> dict:
    return {
        "schema_version": 1,
        "snapshot_kind": "resource-snapshot",
        "snapshot_revision": "sha256:" + "1" * 64,
        "target_id": TARGET,
        "status": "ready",
        "available_processors": processors,
        "available_memory_bytes": 8 * 1024**3,
        "default_request_memory_bytes": 4 * 1024**3,
        "observed_allocation_keys": list(observed),
        "reasons": [],
        "created_at": "2026-08-03T11:59:00+00:00",
        "lock_held": True,
        "target_is_idle": (
            not observed if target_is_idle is None else target_is_idle
        ),
    }


def choice(
    name: str,
    processors: int,
    duration: int,
    *,
    duration_p90_seconds: int | None = None,
    memory_bytes: int = 4 * 1024**3,
    success_rate_ppm: int = 1_000_000,
) -> tuple[dict, dict]:
    option = make_execution_option(
        simulation_definition_artifact_id="simulation-definition.fixture",
        simulation_definition_revision=REVISION,
        runnable_package_artifact_id=f"package.{name}",
        runnable_package_revision="sha256:" + f"{processors:x}" * 64,
        target_id=TARGET,
        processors=processors,
        memory_bytes=memory_bytes,
        performance_class_id=PERFORMANCE_CLASS,
    )
    profile = make_performance_profile(
        execution_option_id=option["option_id"],
        evidence_artifact_id="evidence.performance.fixture",
        evidence_revision=REVISION,
        sample_count=4,
        duration_p50_seconds=duration,
        duration_p90_seconds=(
            duration + 10
            if duration_p90_seconds is None
            else duration_p90_seconds
        ),
        peak_rss_p90_bytes=2 * 1024**3,
        performance_class_id=PERFORMANCE_CLASS,
        success_rate_ppm=success_rate_ppm,
    )
    return option, profile


def option_candidate(attempt_id: str, *choices: tuple[dict, dict]) -> dict:
    options = [item[0] for item in choices]
    profiles = [item[1] for item in choices]
    return {
        "attempt_id": attempt_id,
        "execution_option_set": make_execution_option_set(list(options)),
        "performance_profile_snapshot": make_performance_profile_snapshot(
            policy_revision=REVISION,
            profiles=profiles,
        ),
    }


def ranked_candidate(
    attempt_id: str,
    choice_value: tuple[dict, dict],
    *,
    priority: str = "normal",
    queued_since: str = "2026-08-03T11:30:00+00:00",
) -> dict:
    return {
        **option_candidate(attempt_id, choice_value),
        "evaluation_id": "evaluation:" + attempt_id.removeprefix("attempt:"),
        "priority": priority,
        "queued_since": queued_since,
    }


def ranked_schedule(*candidates: dict) -> dict:
    return schedule(
        list(candidates),
        [],
        snapshot(),
        scheduling_policy=SCHEDULING_POLICY,
        decision_time=DECISION_TIME,
    )


class SchedulingTests(unittest.TestCase):
    def test_legacy_candidate_keeps_v1_decision_shape(self) -> None:
        decision = schedule_legacy_v1([candidate("attempt:legacy", 2)], [], snapshot())

        self.assertEqual(decision["schema_version"], 1)
        self.assertNotIn("option_policy", decision)
        self.assertNotIn("selected_execution_option", decision)
        self.assertEqual(
            scheduling_decision_plain(decision), validate_scheduling_decision(decision)
        )

    def test_skips_large_head_when_later_task_fits(self) -> None:
        decision = schedule_legacy_v1(
            [candidate("attempt:large", 8), candidate("attempt:small", 2)],
            [],
            snapshot(),
        )

        self.assertEqual(decision["action"], "launch")
        self.assertEqual(decision["selected_attempt_id"], "attempt:small")
        self.assertEqual(decision["allocation"]["processors"], 2)
        self.assertEqual(
            scheduling_decision_plain(decision["considered"]),
            [
                {
                    "attempt_id": "attempt:large",
                    "reason_code": "insufficient-processors",
                },
                {"attempt_id": "attempt:small", "reason_code": "selected"},
            ],
        )

    def test_unobserved_active_allocation_is_reserved_once(self) -> None:
        allocation = {
            "attempt_id": "attempt:running",
            "target_id": TARGET,
            "processors": 2,
            "memory_bytes": 4 * 1024**3,
            "resource_key": "/remote/run-1",
        }
        blocked = schedule_legacy_v1(
            [candidate("attempt:new", 4)],
            [allocation],
            snapshot(processors=4),
        )
        visible = schedule_legacy_v1(
            [candidate("attempt:new", 4)],
            [allocation],
            snapshot(processors=4, observed=("/remote/run-1",)),
        )

        self.assertEqual(blocked["action"], "wait")
        self.assertEqual(visible["action"], "launch")

    def test_same_snapshot_produces_same_decision(self) -> None:
        candidates = [candidate("attempt:first", 2), candidate("attempt:second", 1)]
        resources = snapshot()

        self.assertEqual(
            schedule_legacy_v1(candidates, [], resources),
            schedule_legacy_v1(candidates, [], resources),
        )

    def test_throughput_policy_minimizes_estimated_core_seconds(self) -> None:
        p1 = choice("p1", 1, 100)
        p2 = choice("p2", 2, 60)
        p4 = choice("p4", 4, 40)

        decision = schedule(
            [option_candidate("attempt:multi", p4, p1, p2)],
            [],
            snapshot(),
        )

        self.assertEqual(decision["schema_version"], 2)
        self.assertEqual(decision["option_policy"], "throughput")
        self.assertEqual(decision["selected_execution_option"], p1[0])
        self.assertEqual(decision["selected_performance_profile"], p1[1])
        self.assertEqual(decision["allocation"]["processors"], 1)
        self.assertEqual(
            scheduling_decision_plain(decision), validate_scheduling_decision(decision)
        )

    def test_throughput_and_latency_policies_choose_different_fitting_options(self) -> None:
        p1 = choice("policy-p1", 1, 100)
        p2 = choice("policy-p2", 2, 60)
        candidates = [option_candidate("attempt:policy", p1, p2)]
        resources = snapshot(processors=2)

        throughput = schedule(candidates, [], resources, option_policy="throughput")
        latency = schedule(candidates, [], resources, option_policy="latency")

        self.assertEqual(throughput["action"], "launch")
        self.assertEqual(latency["action"], "launch")
        self.assertEqual(throughput["selected_execution_option"], p1[0])
        self.assertEqual(latency["selected_execution_option"], p2[0])
        self.assertNotEqual(throughput["selected_execution_option"], latency["selected_execution_option"])

    def test_latency_policy_selects_fastest_option_that_fits(self) -> None:
        p1 = choice("p1", 1, 100)
        p2 = choice("p2", 2, 60)
        p4 = choice("p4", 4, 40)

        decision = schedule(
            [option_candidate("attempt:multi", p4, p1, p2)],
            [],
            snapshot(processors=2),
            option_policy="latency",
        )

        self.assertEqual(decision["selected_execution_option"], p2[0])
        allocation = make_resource_allocation(
            decision,
            session_ref="session:multi",
            run_id="20260731-120000-001",
            remote_workspace_root="/remote/test-workspace",
            decision_artifact_id="evidence.scheduling-decision.test",
            decision_artifact_path="data/outputs/decision.json",
        )
        self.assertEqual(allocation["processors"], 2)

    def test_calibration_selects_the_task_attested_processor_shape(self) -> None:
        p1 = choice("p1", 1, 100)
        p2 = choice("p2", 2, 80)
        p4 = choice("p4", 4, 60)
        calibration = make_parallel_efficiency_calibration(
            replicate_ordinal=2,
            selected_processors=2,
            unmeasured_processors=[2, 4],
        )

        decision = schedule(
            [
                {
                    **option_candidate("attempt:calibration", p1, p2, p4),
                    "calibration": calibration,
                }
            ],
            [],
            snapshot(),
        )

        self.assertEqual(decision["selected_execution_option"], p2[0])
        self.assertEqual(decision["allocation"]["processors"], 2)

    def test_calibration_reserves_an_idle_target_before_normal_work(self) -> None:
        p1 = choice("p1", 1, 100)
        p2 = choice("p2", 2, 80)
        calibration = make_parallel_efficiency_calibration(
            replicate_ordinal=2,
            selected_processors=2,
            unmeasured_processors=[2],
        )

        decision = schedule(
            [
                option_candidate("attempt:normal", p1),
                {
                    **option_candidate("attempt:calibration", p1, p2),
                    "calibration": calibration,
                },
            ],
            [],
            snapshot(),
        )

        self.assertEqual(decision["selected_attempt_id"], "attempt:calibration")
        self.assertIn(
            {
                "attempt_id": "attempt:normal",
                "reason_code": "target-isolation-reserved",
            },
            decision["considered"],
        )

    def test_active_calibration_exclusively_blocks_other_work(self) -> None:
        active = {
            "attempt_id": "attempt:calibration",
            "target_id": TARGET,
            "processors": 2,
            "memory_bytes": 4 * 1024**3,
            "resource_key": "/remote/calibration",
            "exclusive_target": True,
        }

        decision = schedule(
            [option_candidate("attempt:normal", choice("p1", 1, 100))],
            [active],
            snapshot(observed=("/remote/calibration",)),
        )

        self.assertEqual(decision["action"], "wait")
        self.assertEqual(
            decision["reason_code"], "target-isolation-active-calibration"
        )

    def test_calibration_waits_until_an_existing_target_allocation_drains(self) -> None:
        p1 = choice("p1", 1, 100)
        p2 = choice("p2", 2, 80)
        calibration = make_parallel_efficiency_calibration(
            replicate_ordinal=2,
            selected_processors=2,
            unmeasured_processors=[2],
        )
        active = {
            "attempt_id": "attempt:normal-active",
            "target_id": TARGET,
            "processors": 1,
            "memory_bytes": 4 * 1024**3,
            "resource_key": "/remote/normal",
        }

        decision = schedule(
            [
                {
                    **option_candidate("attempt:calibration", p1, p2),
                    "calibration": calibration,
                }
            ],
            [active],
            snapshot(observed=("/remote/normal",)),
        )

        self.assertEqual(decision["action"], "wait")
        self.assertEqual(
            decision["reason_code"], "target-isolation-awaiting-idle-target"
        )

    def test_calibration_requires_remote_idle_attestation(self) -> None:
        p1 = choice("p1", 1, 100)
        p2 = choice("p2", 2, 80)
        calibration = make_parallel_efficiency_calibration(
            replicate_ordinal=2,
            selected_processors=2,
            unmeasured_processors=[2],
        )

        decision = schedule(
            [
                {
                    **option_candidate("attempt:calibration", p1, p2),
                    "calibration": calibration,
                }
            ],
            [],
            snapshot(target_is_idle=False),
        )

        self.assertEqual(decision["action"], "wait")
        self.assertEqual(
            decision["reason_code"], "target-isolation-idle-not-attested"
        )

    def test_option_candidate_preserves_queue_order_and_skip_nonfit(self) -> None:
        too_large = choice("p4", 4, 40)
        later = choice("p1", 1, 100)

        decision = schedule(
            [
                option_candidate("attempt:large", too_large),
                option_candidate("attempt:small", later),
            ],
            [],
            snapshot(processors=2),
        )

        self.assertEqual(decision["selected_attempt_id"], "attempt:small")
        self.assertEqual(
            scheduling_decision_plain(decision["considered"]),
            [
                {
                    "attempt_id": "attempt:large",
                    "reason_code": "insufficient-processors",
                },
                {"attempt_id": "attempt:small", "reason_code": "selected"},
            ],
        )

    def test_ranked_candidate_requires_central_policy_and_decision_time(self) -> None:
        prepared = ranked_candidate("attempt:ranked", choice("ranked", 1, 100))

        with self.assertRaisesRegex(SchedulingError, "scheduling_policy"):
            schedule([prepared], [], snapshot(), decision_time=DECISION_TIME)
        with self.assertRaisesRegex(SchedulingError, "decision_time"):
            schedule(
                [prepared],
                [],
                snapshot(),
                scheduling_policy=SCHEDULING_POLICY,
            )

    def test_ranked_prepared_decision_is_candidate_permutation_invariant(self) -> None:
        urgent = ranked_candidate(
            "attempt:urgent",
            choice("urgent", 1, 180),
            priority="urgent",
        )
        normal = ranked_candidate(
            "attempt:normal",
            choice("normal", 1, 30),
        )

        forward = ranked_schedule(urgent, normal)
        reverse = ranked_schedule(normal, urgent)

        self.assertEqual(forward, reverse)
        self.assertEqual(forward["selected_attempt_id"], "attempt:urgent")
        self.assertEqual(
            [item["attempt_id"] for item in forward["considered"]],
            ["attempt:normal", "attempt:urgent"],
        )

    def test_unknown_requested_priority_uses_policy_default(self) -> None:
        unknown = ranked_candidate(
            "attempt:unknown",
            choice("unknown", 1, 200),
            priority="algorithm-private-priority",
        )
        low = ranked_candidate(
            "attempt:low",
            choice("low", 1, 10),
            priority="low",
        )

        decision = ranked_schedule(low, unknown)

        self.assertEqual(decision["selected_attempt_id"], "attempt:unknown")

    def test_ranked_prepared_candidate_sorting_factors(self) -> None:
        cases = [
            (
                "priority-order",
                ranked_candidate(
                    "attempt:priority-winner",
                    choice("priority-winner", 1, 150),
                    priority="urgent",
                ),
                ranked_candidate(
                    "attempt:priority-loser",
                    choice("priority-loser", 1, 20),
                ),
                "attempt:priority-winner",
            ),
            (
                "negative-wait-bucket",
                ranked_candidate(
                    "attempt:wait-winner",
                    choice("wait-winner", 1, 150),
                    queued_since="2026-08-03T09:30:00+00:00",
                ),
                ranked_candidate(
                    "attempt:wait-loser",
                    choice("wait-loser", 1, 20),
                ),
                "attempt:wait-winner",
            ),
            (
                "p90",
                ranked_candidate(
                    "attempt:p90-winner",
                    choice(
                        "p90-winner",
                        1,
                        90,
                        duration_p90_seconds=100,
                    ),
                ),
                ranked_candidate(
                    "attempt:p90-loser",
                    choice(
                        "p90-loser",
                        1,
                        10,
                        duration_p90_seconds=110,
                    ),
                ),
                "attempt:p90-winner",
            ),
            (
                "completion-wall",
                ranked_candidate(
                    "attempt:core-loser",
                    choice(
                        "core-loser",
                        1,
                        80,
                        duration_p90_seconds=100,
                    ),
                ),
                ranked_candidate(
                    "attempt:core-winner",
                    choice(
                        "core-winner",
                        2,
                        30,
                        duration_p90_seconds=100,
                    ),
                ),
                "attempt:core-loser",
            ),
            (
                "processors",
                ranked_candidate(
                    "attempt:processors-winner",
                    choice(
                        "processors-winner",
                        1,
                        60,
                        duration_p90_seconds=100,
                    ),
                ),
                ranked_candidate(
                    "attempt:processors-loser",
                    choice(
                        "processors-loser",
                        2,
                        30,
                        duration_p90_seconds=100,
                    ),
                ),
                "attempt:processors-winner",
            ),
            (
                "memory",
                ranked_candidate(
                    "attempt:memory-winner",
                    choice(
                        "memory-winner",
                        1,
                        60,
                        duration_p90_seconds=100,
                        memory_bytes=3 * 1024**3,
                    ),
                ),
                ranked_candidate(
                    "attempt:memory-loser",
                    choice(
                        "memory-loser",
                        1,
                        60,
                        duration_p90_seconds=100,
                    ),
                ),
                "attempt:memory-winner",
            ),
            (
                "queued-since",
                ranked_candidate(
                    "attempt:queued-winner",
                    choice(
                        "queued-winner",
                        1,
                        60,
                        duration_p90_seconds=100,
                    ),
                    queued_since="2026-08-03T11:10:00+00:00",
                ),
                ranked_candidate(
                    "attempt:queued-loser",
                    choice(
                        "queued-loser",
                        1,
                        60,
                        duration_p90_seconds=100,
                    ),
                    queued_since="2026-08-03T11:20:00+00:00",
                ),
                "attempt:queued-winner",
            ),
            (
                "attempt-id",
                ranked_candidate(
                    "attempt:a",
                    choice("shared", 1, 60, duration_p90_seconds=100),
                ),
                ranked_candidate(
                    "attempt:b",
                    choice("shared", 1, 60, duration_p90_seconds=100),
                ),
                "attempt:a",
            ),
        ]

        for label, first, second, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(
                    ranked_schedule(second, first)["selected_attempt_id"],
                    expected,
                )

    def test_p90_backfills_within_the_same_wait_aging_bucket(self) -> None:
        older_slow = ranked_candidate(
            "attempt:older-slow",
            choice("older-slow", 1, 250, duration_p90_seconds=300),
            queued_since="2026-08-03T11:01:00+00:00",
        )
        newer_fast = ranked_candidate(
            "attempt:newer-fast",
            choice("newer-fast", 1, 20, duration_p90_seconds=30),
            queued_since="2026-08-03T11:50:00+00:00",
        )

        decision = ranked_schedule(older_slow, newer_fast)

        self.assertEqual(decision["selected_attempt_id"], "attempt:newer-fast")

    def test_unknown_option_policy_is_rejected(self) -> None:
        with self.assertRaises(SchedulingError):
            schedule([], [], snapshot(), option_policy="algorithm-specific")

    def test_license_capacity_counts_active_allocations_on_other_targets(self) -> None:
        prepared = option_candidate("attempt:new", choice("new", 1, 10))
        other_target_allocations = [
            {
                "attempt_id": f"attempt:other-{ordinal}",
                "target_id": OTHER_TARGET,
                "processors": 4,
                "memory_bytes": 16 * 1024**3,
                "resource_key": f"/remote/other-{ordinal}",
            }
            for ordinal in (1, 2)
        ]
        decision = schedule(
            [prepared],
            other_target_allocations,
            snapshot(processors=4),
            capacity_envelope={
                "processors": 4,
                "memory_bytes": 8 * 1024**3,
                "license_sessions": 2,
            },
        )

        self.assertEqual(decision["action"], "wait")
        self.assertEqual(decision["reason_code"], "insufficient-license-sessions")
        self.assertEqual(
            decision["considered"][0]["reason_code"],
            "insufficient-license-sessions",
        )

    def test_other_target_allocations_do_not_affect_target_capacity_or_isolation(self) -> None:
        active_other_target = {
            "attempt_id": "attempt:other",
            "target_id": OTHER_TARGET,
            "processors": 99,
            "memory_bytes": 99 * 1024**3,
            "resource_key": "/remote/other",
            "exclusive_target": True,
        }
        normal = schedule(
            [option_candidate("attempt:normal", choice("normal", 1, 10))],
            [active_other_target],
            snapshot(processors=1, target_is_idle=False),
        )

        self.assertEqual(normal["action"], "launch")
        self.assertEqual(normal["selected_target_id"], TARGET)

    def test_license_envelope_blocks_second_launch(self) -> None:
        prepared = option_candidate("attempt:one", choice("one", 1, 10))
        envelope = {"processors": 4, "memory_bytes": 8 * 1024**3, "license_sessions": 1}
        first = schedule([prepared], [], snapshot(), capacity_envelope=envelope)
        self.assertEqual(first["action"], "launch")
        active = {
            "attempt_id": "attempt:one",
            "target_id": TARGET,
            "processors": 1,
            "memory_bytes": 4 * 1024**3,
            "resource_key": "/remote/one",
        }
        second = schedule(
            [option_candidate("attempt:two", choice("two", 1, 10))],
            [active],
            snapshot(observed=("/remote/one",)),
            capacity_envelope=envelope,
        )
        self.assertEqual(second["action"], "wait")
        self.assertEqual(second["considered"][0]["reason_code"], "insufficient-license-sessions")

    def test_license_reserve_uses_platform_share(self) -> None:
        active = {
            "attempt_id": "attempt:reserved",
            "target_id": OTHER_TARGET,
            "processors": 1,
            "memory_bytes": 1,
            "resource_key": "/remote/reserved",
        }
        decision = schedule(
            [option_candidate("attempt:new", choice("new", 1, 10))],
            [active],
            snapshot(),
            capacity_envelope={
                "processors": 4,
                "memory_bytes": 8 * 1024**3,
                "license_sessions": 2,
                "license_reserve": 1,
            },
        )
        self.assertEqual(decision["action"], "wait")
        self.assertEqual(decision["reason_code"], "insufficient-license-sessions")

    def test_license_sentinel_blocks_an_external_pool_occupancy(self) -> None:
        resources = snapshot()
        resources["license_sessions_in_use"] = 2
        decision = schedule(
            [option_candidate("attempt:external", choice("external", 1, 10))],
            [],
            resources,
            capacity_envelope={
                "processors": 4,
                "memory_bytes": 8 * 1024**3,
                "license_sessions": 2,
            },
        )
        self.assertEqual(decision["action"], "wait")
        self.assertEqual(decision["reason_code"], "license-pool-exhausted")

    def test_missing_license_sentinel_does_not_change_ledger_decision(self) -> None:
        prepared = option_candidate("attempt:sentinel-missing", choice("missing", 1, 10))
        envelope = {"processors": 4, "memory_bytes": 8 * 1024**3, "license_sessions": 2}
        without_field = schedule([prepared], [], snapshot(), capacity_envelope=envelope)
        with_none = snapshot()
        with_none["license_sessions_in_use"] = None
        self.assertEqual(
            schedule([prepared], [], with_none, capacity_envelope=envelope)["action"],
            without_field["action"],
        )

    def test_policy_aggregate_cap_overrides_snapshot_capacity(self) -> None:
        prepared = option_candidate("attempt:capped", choice("capped", 2, 10))
        decision = schedule(
            [prepared], [], snapshot(processors=8),
            capacity_envelope={
                "processors": 1,
                "memory_bytes": 4 * 1024**3,
                "license_sessions": 1,
            },
        )
        self.assertEqual(decision["action"], "wait")

    def test_ready_snapshot_without_monitor_attestation_waits(self) -> None:
        prepared = option_candidate("attempt:unattested", choice("unattested", 1, 10))
        for label, resources in (
            ("missing-lock", {key: value for key, value in snapshot().items() if key != "lock_held"}),
            ("false-lock", {**snapshot(), "lock_held": False}),
            ("blocking-reason", {**snapshot(), "reasons": ["fixture-blocked"]}),
        ):
            with self.subTest(label=label):
                decision = schedule([prepared], [], resources)
                self.assertEqual(decision["action"], "wait")
                self.assertEqual(decision["reason_code"], "resource-snapshot-blocked")

    def test_returned_decision_is_deeply_immutable_and_plain_roundtrips(self) -> None:
        decision = schedule([option_candidate("attempt:frozen", choice("frozen", 1, 10))], [], snapshot())
        with self.assertRaises(TypeError):
            decision["action"] = "wait"
        with self.assertRaises(TypeError):
            decision["considered"][0]["reason_code"] = "changed"
        plain = scheduling_decision_plain(decision)
        self.assertIsInstance(plain, dict)
        self.assertIsInstance(plain["considered"], list)
        self.assertEqual(plain, validate_scheduling_decision(decision))

    def test_candidate_active_overlap_and_duplicate_resource_key_fail_closed(self) -> None:
        prepared = option_candidate("attempt:same", choice("same", 1, 10))
        active = {
            "attempt_id": "attempt:same",
            "target_id": TARGET,
            "processors": 1,
            "memory_bytes": 4 * 1024**3,
            "resource_key": "/remote/same",
        }
        with self.assertRaisesRegex(SchedulingError, "already present"):
            schedule([prepared], [active], snapshot())
        duplicate = {**active, "attempt_id": "attempt:other"}
        with self.assertRaisesRegex(SchedulingError, "duplicate"):
            schedule([], [active, duplicate], snapshot())

    def test_permuted_active_and_observed_inputs_have_same_decision_id(self) -> None:
        first = {
            "attempt_id": "attempt:first",
            "target_id": TARGET,
            "processors": 1,
            "memory_bytes": 1 * 1024**3,
            "resource_key": "/remote/first",
        }
        second = {**first, "attempt_id": "attempt:second", "resource_key": "/remote/second"}
        prepared = option_candidate("attempt:new", choice("new", 1, 4))
        envelope = {"processors": 8, "memory_bytes": 16 * 1024**3, "license_sessions": 4}
        forward = schedule(
            [prepared], [first, second],
            snapshot(observed=("/remote/second", "/remote/first")),
            capacity_envelope=envelope,
        )
        reverse = schedule(
            [prepared], [second, first],
            snapshot(observed=("/remote/first", "/remote/second")),
            capacity_envelope=envelope,
        )
        self.assertEqual(forward["decision_id"], reverse["decision_id"])

    def test_raw_legacy_candidate_is_rejected_by_strict_schedule(self) -> None:
        with self.assertRaisesRegex(SchedulingError, "prepared execution option set"):
            schedule([candidate("attempt:legacy", 1)], [], snapshot())

    def test_legacy_and_prepared_candidates_cannot_share_one_decision(self) -> None:
        prepared = option_candidate("attempt:prepared", choice("p1", 1, 100))

        with self.assertRaisesRegex(SchedulingError, "legacy candidate"):
            schedule_legacy_v1(
                [candidate("attempt:legacy", 1), prepared],
                [],
                snapshot(),
            )


if __name__ == "__main__":
    unittest.main()
