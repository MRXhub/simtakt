"""Shared fixtures for tests that persist problem definitions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from control_plane.evaluation.parameter_schema import make_parameter_schema


def register_fixture_schema(middleware: Any, *, problem_hint: str = "fixture") -> str:
    """Register a minimal valid parameter schema and return its computed revision."""
    schema = make_parameter_schema(
        parameters=[
            {"name": "x", "type": "float", "role": "variable", "bounds": {"min": 0.0, "max": 1.0}}
        ],
        problem_hint=problem_hint,
        source_package={
            "artifact_id": "package.fixture.v1",
            "revision": "sha256:" + "f" * 64,
        },
    )
    register = getattr(middleware, "register_schema", None)
    if register is None:
        register = middleware.register_schema_document
    return register(schema)["revision"]


DEFAULT_SCHEDULING_POLICY = {
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


def write_governed_project(
    root: Path | str,
    *,
    policy: dict[str, Any] | None = None,
    artifact_id: str = "configuration.project-scheduling-policy.default-v1",
    policy_relpath: str = "project/scheduling-policy.json",
    include_scheduling_policy: bool = True,
    write_project_state: bool = True,
) -> tuple[str, str]:
    """Write a project whose scheduling policy binds via RUNTIME_COMPONENTS.json.

    The policy resolution path now reads ``project/RUNTIME_COMPONENTS.json``
    (not PROJECT_STATE.json) and treats its ``scheduling_policy`` reference as
    authoritative.  This helper installs the policy document, its artifact
    registry shard, and the assembly document together so that
    ``resolve_governed_scheduling_policy`` succeeds against a temp project.
    Returns ``(artifact_id, revision)``.
    """
    root = Path(root)
    body = DEFAULT_SCHEDULING_POLICY if policy is None else policy
    policy_path = root / policy_relpath
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(json.dumps(body), encoding="utf-8")
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
                                    "path": policy_relpath,
                                }
                            ],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    # The scheduling policy binding now lives in the runtime assembly document.
    # Omitting it (include_scheduling_policy=False) leaves no components file so
    # resolution fails closed with the explicit scheduling-policy-not-configured
    # block rather than a stale PROJECT_STATE reference.
    if include_scheduling_policy:
        components_path = root / "project" / "RUNTIME_COMPONENTS.json"
        components_path.parent.mkdir(parents=True, exist_ok=True)
        components_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "scheduling_policy": {
                        "artifact_id": artifact_id,
                        "revision": revision,
                    },
                }
            ),
            encoding="utf-8",
        )

    if write_project_state:
        state: dict[str, Any] = {"schema_version": 2, "status": "active"}
        if include_scheduling_policy:
            state["scheduling_policy"] = {
                "artifact_id": artifact_id,
                "revision": revision,
                "status": "active",
            }
        state_path = root / "project" / "PROJECT_STATE.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state), encoding="utf-8")

    return artifact_id, revision
