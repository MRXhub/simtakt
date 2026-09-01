#!/usr/bin/env python3
"""Strict project SchedulingPolicy authority checks."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

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
    }


def write_project(root: Path, *, include_reference: bool = True) -> tuple[str, str]:
    artifact_id = "configuration.project-scheduling-policy.default-v1"
    policy_path = root / "project" / "scheduling-policy.json"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(json.dumps(policy_body()), encoding="utf-8")
    revision = "sha256:" + hashlib.sha256(policy_path.read_bytes()).hexdigest()
    shard_path = root / "records" / "artifacts" / f"{artifact_id}.json"
    shard_path.parent.mkdir(parents=True, exist_ok=True)
    shard_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "record_kind": "artifact-catalog-shard",
                "artifact": {
                    "artifact_id": artifact_id,
                    "kind": "configuration",
                    "status": "active",
                    "latest_revision": revision,
                    "revisions": [
                        {
                            "revision": revision,
                            "hash_scope": "file",
                            "locations": [
                                {
                                    "storage": "workspace",
                                    "role": "primary",
                                    "availability": "required",
                                    "path": "project/scheduling-policy.json",
                                }
                            ],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    state = {"schema_version": 2, "status": "active"}
    if include_reference:
        state["scheduling_policy"] = {
            "artifact_id": artifact_id,
            "revision": revision,
            "status": "active",
        }
    (root / "project" / "PROJECT_STATE.json").write_text(
        json.dumps(state), encoding="utf-8"
    )
    return artifact_id, revision


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
