#!/usr/bin/env python3
"""Strict project SchedulingPolicy authority checks."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.shared_fixtures import write_governed_project
from control_plane.evaluation.scheduling_policy import (
    GovernedSchedulingPolicy,
    SchedulingPolicyBlocked,
    SchedulingPolicyError,
    capacity_slots,
    preparation_window_limit,
    resolve_governed_scheduling_policy,
    validate_scheduling_policy,
)


def policy_body() -> dict:
    return {
        "schema_version": 1,
        "policy_kind": "project-scheduling-policy",
        "status": "active",
        "capacity_envelope": {
            "processors": 16,
            "memory_bytes": 32 * 1024**3,
            "license_sessions": 6,
            "baseline_processors": 1,
            "baseline_memory_bytes": 4 * 1024**3,
        },
        "priority_order": ["high", "normal", "low"],
        "default_priority": "normal",
        "aging_quantum_seconds": 3600,
        "preparation_claim_seconds": 120,
        "option_policy": "throughput",
        "kill_multiplier": 1.7,
        "stall_fraction": 0.25,
        "min_budget_samples": 5,
        "kill_rate_widen_threshold": 0.10,
        "kill_widen_factor": 1.5,
        "reconcile_hold_seconds": 1800,
        "orphan_ttl_seconds": 604800,
        "orphans_hold_license": True,
        "orphan_batch_size": 10,
    }


def write_project(root: Path, *, include_reference: bool = True) -> tuple[str, str]:
    """Write a governed project whose scheduling policy binds via
    RUNTIME_COMPONENTS.json."""
    artifact_id = "configuration.project-scheduling-policy.default-v1"
    return write_governed_project(
        root,
        artifact_id=artifact_id,
        include_scheduling_policy=include_reference,
    )


class SchedulingPolicyTests(unittest.TestCase):
    def test_capacity_and_window_use_all_three_envelopes(self) -> None:
        policy = policy_body()
        self.assertEqual(capacity_slots(policy), 6)
        self.assertEqual(preparation_window_limit(policy), 9)

    def test_resolves_exact_root_reference_to_opaque_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact_id, revision = write_project(root)

            governed = resolve_governed_scheduling_policy(root)

            self.assertIsInstance(governed, GovernedSchedulingPolicy)
            self.assertTrue(governed.is_attested_for(root))
            self.assertEqual(governed.capacity_slots, 6)
            self.assertEqual(governed.window_limit, 9)
            self.assertEqual(
                governed.provenance()["artifact_id"], artifact_id
            )
            self.assertEqual(governed.provenance()["revision"], revision)
            self.assertEqual(governed.as_mapping(), policy_body())

    def test_missing_root_reference_is_explicitly_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_project(root, include_reference=False)

            with self.assertRaises(SchedulingPolicyBlocked) as caught:
                resolve_governed_scheduling_policy(root)

            self.assertEqual(
                caught.exception.reason_code,
                "scheduling-policy-not-configured",
            )

    def test_contract_rejects_unknown_fields_and_invalid_default(self) -> None:
        extra = {**policy_body(), "target_id": "caller-selected"}
        with self.assertRaises(SchedulingPolicyError):
            validate_scheduling_policy(extra)

        invalid_default = policy_body()
        invalid_default["default_priority"] = "urgent"
        with self.assertRaises(SchedulingPolicyError):
            validate_scheduling_policy(invalid_default)

    def test_contract_rejects_boolean_capacity_and_oversized_baseline(self) -> None:
        boolean = policy_body()
        boolean["capacity_envelope"]["processors"] = True
        with self.assertRaises(SchedulingPolicyError):
            validate_scheduling_policy(boolean)

        oversized = policy_body()
        oversized["capacity_envelope"]["baseline_processors"] = 17
        with self.assertRaises(SchedulingPolicyError):
            validate_scheduling_policy(oversized)

    def test_license_reserve_is_optional_bounded_and_not_boolean(self) -> None:
        legacy = validate_scheduling_policy(policy_body())
        self.assertNotIn("license_reserve", legacy["capacity_envelope"])

        reserved = policy_body()
        reserved["capacity_envelope"]["license_reserve"] = 2
        self.assertEqual(
            validate_scheduling_policy(reserved)["capacity_envelope"]["license_reserve"],
            2,
        )
        for invalid in (-1, 6, True):
            candidate = policy_body()
            candidate["capacity_envelope"]["license_reserve"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(SchedulingPolicyError):
                validate_scheduling_policy(candidate)

    def test_policy_constructor_rejects_unsealed_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(SchedulingPolicyError):
                GovernedSchedulingPolicy(
                    policy_body(),
                    project_root=Path(temporary),
                    artifact_id="configuration.project-scheduling-policy.default-v1",
                    artifact_revision="sha256:" + "1" * 64,
                    project_state_revision="sha256:" + "2" * 64,
                    _seal=object(),
                )


if __name__ == "__main__":
    unittest.main()
