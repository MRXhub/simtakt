#!/usr/bin/env python3
"""Authority checks for project-policy-derived Preparations."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from control_plane.evaluation import governed_preparation
from control_plane.evaluation.execution_options import (
    make_execution_option,
    make_execution_option_set,
    make_parallel_efficiency_calibration,
    make_execution_preparation,
    make_performance_profile,
    make_performance_profile_snapshot,
)
from control_plane.evaluation.governed_preparation import (
    GovernedExecutionPreparation,
    _authorization,
    GovernedPreparationError,
    attest_policy_derived_execution_preparation,
    validate_policy_derived_execution_preparation,
)
from tests.shared_fixtures import write_governed_project
from control_plane.evaluation.scheduling_policy import (
    resolve_governed_scheduling_policy,
)


REVISION = "sha256:" + "a" * 64
EVALUATION_ID = "evaluation:fixture"
CANDIDATE_ID = "candidate:sha256:" + "1" * 64
TASK_ID = "fixture-task"
TARGET_ID = "silvaco.fixture"
PERFORMANCE_CLASS = "performance-class:sha256:" + "b" * 64


def _policy_body() -> dict:
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


def _write_project(root: Path, *, policy: dict | None = None) -> None:
    """Write a governed project (scheduling policy bound in
    RUNTIME_COMPONENTS.json) plus the policy-derived task envelope."""
    write_governed_project(
        root,
        policy=policy,
        artifact_id="configuration.project-scheduling-policy.fixture-v1",
    )
    state = {
        "schema_version": 2,
        "status": "active",
        "active_tasks": [
            {
                "id": TASK_ID,
                "kind": "simulation",
                "status": "approved-prepared-execution",
            }
        ],
    }
    state_path = root / "project" / "PROJECT_STATE.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")


def _preparation(*, processors: int = 1, memory_bytes: int = 4 * 1024**3) -> dict:
    option = make_execution_option(
        simulation_definition_artifact_id="simulation-definition.fixture",
        simulation_definition_revision=REVISION,
        runnable_package_artifact_id="package.fixture",
        runnable_package_revision=REVISION,
        target_id=TARGET_ID,
        processors=processors,
        memory_bytes=memory_bytes,
        performance_class_id=PERFORMANCE_CLASS,
    )
    profile = make_performance_profile(
        execution_option_id=option["option_id"],
        evidence_artifact_id="evidence.performance.fixture",
        evidence_revision=REVISION,
        sample_count=3,
        duration_p50_seconds=100,
        duration_p90_seconds=120,
        peak_rss_p90_bytes=min(memory_bytes, 2 * 1024**3),
        performance_class_id=PERFORMANCE_CLASS,
    )
    return make_execution_preparation(
        evaluation_id=EVALUATION_ID,
        candidate_id=CANDIDATE_ID,
        simulation_proxy="silvaco-session-v1",
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


def _evaluation_input() -> dict:
    return {
        "candidate": {"candidate_id": CANDIDATE_ID},
        "evaluation": {
            "evaluation_id": EVALUATION_ID,
            "candidate_id": CANDIDATE_ID,
            "status": "queued",
        },
    }


@contextmanager
def _stub_external_execution_authority():
    with (
        patch.object(
            governed_preparation,
            "_formal_targets",
            return_value={TARGET_ID: {"target_id": TARGET_ID}},
        ),
        patch.object(
            governed_preparation,
            "_authorization",
            return_value={"target_id": TARGET_ID},
        ),
        patch.object(governed_preparation, "_validate_options") as validate_options,
    ):
        yield validate_options


class PolicyDerivedPreparationTests(unittest.TestCase):
    def _multi_authorization_case(self, *, duplicate_target: bool = False):
        root = Path(tempfile.mkdtemp())
        targets_path = root / "project" / "EXECUTION_TARGETS.json"
        targets_path.parent.mkdir(parents=True, exist_ok=True)
        target_a = {
            "target_id": "target-a",
            "status": "active",
            "formal_execution": True,
            "workspace_root": "/remote/a",
            "allowed_operations": ["simulation"],
        }
        target_b = {
            "target_id": "target-b",
            "status": "active",
            "formal_execution": True,
            "workspace_root": "/remote/b",
            "allowed_operations": ["simulation"],
        }
        targets_path.write_text(json.dumps({"targets": [target_a, target_b]}), encoding="utf-8")
        expiry = "2099-01-01T00:00:00+00:00"
        refs = [
            {
                "artifact_id": "authorization.a",
                "revision": REVISION,
                "authorization_kind": "prepared-execution-envelope-v1",
                "status": "active",
                "target_id": "target-a",
                "expires_at": expiry,
            },
            {
                "artifact_id": "authorization.b",
                "revision": REVISION,
                "authorization_kind": "prepared-execution-envelope-v1",
                "status": "active",
                "target_id": "target-a" if duplicate_target else "target-b",
                "expires_at": expiry,
            },
        ]
        bodies = {}
        for ref, target in zip(refs, (target_a, target_b)):
            bodies[ref["artifact_id"]] = {
                "authorization_id": ref["artifact_id"],
                "authorization_kind": "prepared-execution-envelope-v1",
                "status": "active",
                "task_id": TASK_ID,
                "expires_at": expiry,
                "scope": {
                    "target_id": ref["target_id"],
                    "execution_targets_revision": "sha256:" + hashlib.sha256(targets_path.read_bytes()).hexdigest(),
                    "max_timeout_seconds": 600,
                    "max_attempts_per_candidate": 1,
                    "allowed_processors": [1, 2, 8],
                    "max_memory_bytes": 8 * 1024**3,
                },
                "execution_target": dict(target),
            }
        options = [
            {"target_id": "target-a", "processors": 1, "memory_bytes": 4 * 1024**3},
            {"target_id": "target-b", "processors": 2, "memory_bytes": 4 * 1024**3},
        ]
        preparation = {
            "authorization": {"artifact_id": "authorization.a", "revision": REVISION},
            "authorizations": [
                {"artifact_id": ref["artifact_id"], "revision": REVISION, "target_id": ref["target_id"]}
                for ref in refs
            ],
            "execution_option_set": {"options": options},
            "budget": {"command_timeout_seconds": 600, "max_solver_runs": 1, "max_wall_seconds": 900},
        }
        task = {"id": TASK_ID, "execution_authorizations": refs}
        targets = {"target-a": target_a, "target-b": target_b}
        return root, task, preparation, targets, bodies

    def test_two_active_authorizations_and_targets_are_accepted(self) -> None:
        root, task, preparation, targets, bodies = self._multi_authorization_case()
        try:
            with patch.object(
                governed_preparation,
                "_resolve_json_artifact",
                side_effect=lambda *args, **kwargs: (bodies[args[1]], Path("fixture")),
            ):
                records = _authorization(
                    root, task, preparation, targets,
                    now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                )
            self.assertEqual([record["target_id"] for record in records], ["target-a", "target-b"])
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_active_authorizations_reject_duplicate_targets(self) -> None:
        root, task, preparation, targets, bodies = self._multi_authorization_case(duplicate_target=True)
        try:
            with patch.object(governed_preparation, "_resolve_json_artifact", side_effect=lambda *args, **kwargs: (bodies[args[1]], Path("fixture"))):
                with self.assertRaisesRegex(GovernedPreparationError, "unique targets"):
                    _authorization(root, task, preparation, targets, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_option_targets_must_exactly_match_active_authorizations(self) -> None:
        root, task, preparation, targets, bodies = self._multi_authorization_case()
        preparation["execution_option_set"]["options"] = preparation["execution_option_set"]["options"][:1]
        try:
            with patch.object(governed_preparation, "_resolve_json_artifact", side_effect=lambda *args, **kwargs: (bodies[args[1]], Path("fixture"))):
                with self.assertRaisesRegex(GovernedPreparationError, "exactly match"):
                    _authorization(root, task, preparation, targets, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_option_is_checked_against_its_own_target_envelope(self) -> None:
        root, task, preparation, targets, bodies = self._multi_authorization_case()
        bodies["authorization.a"]["scope"]["allowed_processors"] = [1]
        preparation["execution_option_set"]["options"][0]["processors"] = 8
        try:
            with patch.object(governed_preparation, "_resolve_json_artifact", side_effect=lambda *args, **kwargs: (bodies[args[1]], Path("fixture"))):
                with self.assertRaisesRegex(GovernedPreparationError, "exceeds its authorization budget"):
                    _authorization(root, task, preparation, targets, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_single_preparation_lineage_must_cover_every_active_authorization(self) -> None:
        root, task, preparation, targets, bodies = self._multi_authorization_case()
        preparation.pop("authorizations")
        try:
            with patch.object(governed_preparation, "_resolve_json_artifact", side_effect=lambda *args, **kwargs: (bodies[args[1]], Path("fixture"))):
                with self.assertRaisesRegex(GovernedPreparationError, "cover every active"):
                    _authorization(root, task, preparation, targets, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_policy_derived_neutral_package_skips_legacy_package_approval(
        self,
    ) -> None:
        preparation = _preparation()
        profile = preparation["performance_profile_snapshot"]["profiles"][0]
        evidence_document = {
            "schema_version": 1,
            "evidence_kind": "execution-performance-evidence",
            "profiles": [
                {
                    key: profile[key]
                    for key in (
                        "performance_class_id",
                        "sample_count",
                        "duration_p50_seconds",
                        "duration_p90_seconds",
                        "peak_rss_p90_bytes",
                        "success_rate_ppm",
                    )
                }
            ],
        }

        def resolve(*args: object, expected_kind: str, **kwargs: object):
            return Mock(
                hash_scope=(
                    "package-manifest"
                    if expected_kind == "input-package"
                    else "file"
                ),
                path=Path("fixture"),
            )

        def read_json(path: Path, label: str):
            if label == "package manifest":
                return {
                    "artifact_id": "package.fixture",
                    "design": {"candidate_id": CANDIDATE_ID},
                }
            return evidence_document

        with (
            patch.object(
                governed_preparation,
                "resolve_workspace_artifact",
                side_effect=resolve,
            ),
            patch.object(governed_preparation, "_read_json", side_effect=read_json),
            patch.object(
                governed_preparation, "_validate_resource_neutral_package"
            ),
            patch.object(
                governed_preparation,
                "_problem_template_package",
                return_value={
                    "artifact_id": "package.fixture",
                    "revision": REVISION,
                },
            ),
        ):
            governed_preparation._validate_options(
                Path("."),
                {"approved_packages": []},
                preparation,
                [],
                require_resource_neutral_package=True,
            )

    def test_attestation_seals_exact_policy_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_project(root)
            policy = resolve_governed_scheduling_policy(root)
            preparation = _preparation()

            with _stub_external_execution_authority():
                governed = attest_policy_derived_execution_preparation(
                    root,
                    _evaluation_input(),
                    preparation,
                    policy,
                )

            self.assertIsInstance(governed, GovernedExecutionPreparation)
            self.assertTrue(governed.is_attested_for(root))
            self.assertEqual(governed.as_mapping(), preparation)
            self.assertEqual(governed.provenance(), policy.provenance())
            with self.assertRaises(GovernedPreparationError):
                GovernedExecutionPreparation(
                    preparation,
                    project_root=root,
                    artifact_id=policy.provenance()["artifact_id"],
                    artifact_revision=policy.provenance()["revision"],
                    project_state_revision=policy.provenance()[
                        "project_state_revision"
                    ],
                    _seal=object(),
                )

    def test_attestation_rejects_evaluation_and_candidate_lineage_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_project(root)
            policy = resolve_governed_scheduling_policy(root)
            preparation = _preparation()
            wrong_candidate = "candidate:sha256:" + "2" * 64
            invalid_inputs = {
                "evaluation": {
                    "candidate": {"candidate_id": CANDIDATE_ID},
                    "evaluation": {
                        "evaluation_id": "evaluation:other",
                        "candidate_id": CANDIDATE_ID,
                        "status": "queued",
                    },
                },
                "candidate": {
                    "candidate": {"candidate_id": wrong_candidate},
                    "evaluation": {
                        "evaluation_id": EVALUATION_ID,
                        "candidate_id": wrong_candidate,
                        "status": "queued",
                    },
                },
            }

            with _stub_external_execution_authority():
                for mismatch, evaluation_input in invalid_inputs.items():
                    with self.subTest(mismatch=mismatch):
                        with self.assertRaises(GovernedPreparationError):
                            attest_policy_derived_execution_preparation(
                                root,
                                evaluation_input,
                                preparation,
                                policy,
                            )

    def test_attestation_rejects_policy_revision_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_project(root)
            stale_policy = resolve_governed_scheduling_policy(root)
            replacement = _policy_body()
            replacement["option_policy"] = "latency"
            _write_project(root, policy=replacement)

            with _stub_external_execution_authority():
                with self.assertRaises(GovernedPreparationError):
                    attest_policy_derived_execution_preparation(
                        root,
                        _evaluation_input(),
                        _preparation(),
                        stale_policy,
                    )

    def test_attestation_rejects_components_revision_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_project(root)
            stale_policy = resolve_governed_scheduling_policy(root)
            # The scheduling policy provenance now binds the sha256 of the
            # runtime assembly document, so a drift in RUNTIME_COMPONENTS.json
            # (not PROJECT_STATE.json) is what attestation must reject.
            components_path = root / "project" / "RUNTIME_COMPONENTS.json"
            components = json.loads(components_path.read_text(encoding="utf-8"))
            components_path.write_text(
                json.dumps(components, indent=2), encoding="utf-8"
            )

            with _stub_external_execution_authority():
                with self.assertRaises(GovernedPreparationError):
                    attest_policy_derived_execution_preparation(
                        root,
                        _evaluation_input(),
                        _preparation(),
                        stale_policy,
                    )

    def test_capacity_envelope_rejects_oversized_option(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_project(root)
            policy = resolve_governed_scheduling_policy(root)

            with _stub_external_execution_authority():
                with self.assertRaisesRegex(
                    GovernedPreparationError,
                    "exceeds the SchedulingPolicy capacity envelope",
                ):
                    attest_policy_derived_execution_preparation(
                        root,
                        _evaluation_input(),
                        _preparation(processors=17),
                        policy,
                    )

    def test_validation_requests_resource_neutral_package_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_project(root)
            preparation = _preparation()

            with _stub_external_execution_authority() as validate_options:
                self.assertEqual(
                    validate_policy_derived_execution_preparation(
                        root, preparation
                    ),
                    preparation,
                )

            validate_options.assert_called_once()
            self.assertIs(
                validate_options.call_args.kwargs[
                    "require_resource_neutral_package"
                ],
                True,
            )

    def test_calibration_must_match_the_task_attested_processor_sequence(self) -> None:
        task = {
            "parallel_efficiency_calibration": {
                "schema_version": 1,
                "calibration_kind": "parallel-efficiency-v1",
                "candidate_id": CANDIDATE_ID,
                "evidence_profile": "parallel-efficiency-v1",
                "replicate_key_prefix": "parallel-efficiency",
                "processor_sequence": [1, 2, 4, 4, 1, 2, 2, 4, 1],
                "unmeasured_processors": [2, 4],
                "fidelity": "full-tcad",
                "requested_outputs": ["Eff"],
                "priority": "high",
                "target_isolation": "exclusive",
            }
        }
        preparation = {
            "candidate_id": CANDIDATE_ID,
            "calibration": make_parallel_efficiency_calibration(
                replicate_ordinal=2,
                selected_processors=2,
                unmeasured_processors=[2, 4],
            )
        }

        self.assertEqual(
            governed_preparation._parallel_efficiency_calibration(task, preparation),
            preparation["calibration"],
        )
        preparation["calibration"] = make_parallel_efficiency_calibration(
            replicate_ordinal=2,
            selected_processors=4,
            unmeasured_processors=[2, 4],
        )
        with self.assertRaisesRegex(
            GovernedPreparationError,
            "differs from its authorized sequence",
        ):
            governed_preparation._parallel_efficiency_calibration(task, preparation)

    def test_resource_neutral_package_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package_root = Path(temporary)
            deck_path = package_root / "device.in"
            manifest = {
                "deck_file": "device.in",
                "execution": {"command_timeout_seconds": 600},
            }
            deck_path.write_bytes(b"go atlas\nsolve init\n")

            governed_preparation._validate_resource_neutral_package(
                package_root, manifest
            )

            for field in ("processors", "atlas_processors"):
                with self.subTest(manifest_field=field):
                    frozen = {
                        **manifest,
                        "execution": {**manifest["execution"], field: 1},
                    }
                    with self.assertRaises(GovernedPreparationError):
                        governed_preparation._validate_resource_neutral_package(
                            package_root, frozen
                        )

    def test_authorization_resolves_real_on_disk_json_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            targets_path = root / "project" / "EXECUTION_TARGETS.json"
            targets_path.parent.mkdir(parents=True, exist_ok=True)
            target_a = {
                "target_id": "target-a",
                "status": "active",
                "formal_execution": True,
                "workspace_root": "/remote/a",
                "allowed_operations": ["simulation"],
            }
            target_b = {
                "target_id": "target-b",
                "status": "active",
                "formal_execution": True,
                "workspace_root": "/remote/b",
                "allowed_operations": ["simulation"],
            }
            targets_path.write_text(
                json.dumps({"targets": [target_a, target_b]}),
                encoding="utf-8",
            )
            expiry = "2099-01-01T00:00:00+00:00"
            targets_revision = (
                "sha256:" + hashlib.sha256(targets_path.read_bytes()).hexdigest()
            )

            refs = []
            authorizations_lineage = []
            for target_id, target in [("target-a", target_a), ("target-b", target_b)]:
                artifact_id = f"authorization.{target_id}"
                auth_path = root / "project" / "authorizations" / f"{artifact_id}.json"
                auth_path.parent.mkdir(parents=True, exist_ok=True)
                body = {
                    "authorization_id": artifact_id,
                    "authorization_kind": "prepared-execution-envelope-v1",
                    "status": "active",
                    "task_id": TASK_ID,
                    "expires_at": expiry,
                    "scope": {
                        "target_id": target_id,
                        "execution_targets_revision": targets_revision,
                        "max_timeout_seconds": 600,
                        "max_attempts_per_candidate": 1,
                        "allowed_processors": [1, 2, 8],
                        "max_memory_bytes": 8 * 1024**3,
                    },
                    "execution_target": dict(target),
                }
                auth_path.write_text(json.dumps(body), encoding="utf-8")
                revision = (
                    "sha256:" + hashlib.sha256(auth_path.read_bytes()).hexdigest()
                )

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
                                                "path": f"project/authorizations/{artifact_id}.json",
                                            }
                                        ],
                                    }
                                ],
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                refs.append(
                    {
                        "artifact_id": artifact_id,
                        "revision": revision,
                        "authorization_kind": "prepared-execution-envelope-v1",
                        "status": "active",
                        "target_id": target_id,
                        "expires_at": expiry,
                    }
                )
                authorizations_lineage.append(
                    {
                        "artifact_id": artifact_id,
                        "revision": revision,
                        "target_id": target_id,
                    }
                )

            options = [
                {"target_id": "target-a", "processors": 1, "memory_bytes": 4 * 1024**3},
                {"target_id": "target-b", "processors": 2, "memory_bytes": 4 * 1024**3},
            ]
            preparation = {
                "authorization": {
                    "artifact_id": "authorization.target-a",
                    "revision": refs[0]["revision"],
                },
                "authorizations": authorizations_lineage,
                "execution_option_set": {"options": options},
                "budget": {
                    "command_timeout_seconds": 600,
                    "max_solver_runs": 1,
                    "max_wall_seconds": 900,
                },
            }
            task = {"id": TASK_ID, "execution_authorizations": refs}
            targets = {"target-a": target_a, "target-b": target_b}

            records = _authorization(
                root,
                task,
                preparation,
                targets,
                now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            self.assertEqual(
                [r["target_id"] for r in records], ["target-a", "target-b"]
            )


if __name__ == "__main__":
    unittest.main()
