"""Execution-target topology declarations and multi-target readiness checks.

The topology is deliberately separate from governed preparation.  In particular,
reading this module never changes or re-writes ``EXECUTION_TARGETS.json`` and
therefore does not alter its authorization revision.
"""
from __future__ import annotations

import json
import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from control_plane.core.ports import TargetCatalog


class ExecutionTopologyError(ValueError):
    """Raised when the execution-target topology is malformed or unusable."""
_TOKEN = re.compile(r"^\S+$")
_DEFAULT_LICENSE_POOL = "default"


class ProjectFileTargetCatalog:
    """Read execution targets from the governed project JSON file."""

    def read_targets(self, project_root: Path | str) -> list[Mapping[str, Any]]:
        value, _ = self.read_targets_with_revision(project_root)
        return value

    def read_targets_with_revision(
        self, project_root: Path | str
    ) -> tuple[list[Mapping[str, Any]], str]:
        path = Path(project_root).resolve() / "project" / "EXECUTION_TARGETS.json"
        try:
            raw = path.read_bytes()
            value = json.loads(raw.decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("cannot read execution targets") from exc
        if not isinstance(value, Mapping) or not isinstance(value.get("targets"), list):
            raise ValueError("execution targets must be an array")
        return value["targets"], "sha256:" + hashlib.sha256(raw).hexdigest()


def _token(value: object, label: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value.strip()):
        raise ExecutionTopologyError(f"{label} must be a non-empty string token")
    return value.strip()


def _read_targets(
    project_root: Path | str,
    target_catalog: TargetCatalog,
) -> list[Mapping[str, Any]]:
    try:
        return list(target_catalog.read_targets(project_root))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ExecutionTopologyError("cannot read execution targets") from exc


def parse_execution_topology(
    project_root: Path | str,
    target_catalog: TargetCatalog | None = None,
) -> dict[str, Any]:

    """Read and normalize target topology declarations from a project.

    Missing ``host_id`` is retained as ``None`` and is intentionally not
    interpreted as a unique host.  Missing ``license_pool_id`` belongs to the
    single, explicit default pool.
    """
    catalog = target_catalog or ProjectFileTargetCatalog()
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(_read_targets(project_root, catalog)):
        if not isinstance(item, Mapping):
            raise ExecutionTopologyError(f"execution target at index {index} is invalid")
        target_id = _token(item.get("target_id"), "execution target target_id")
        if target_id in seen:
            raise ExecutionTopologyError(f"duplicate execution target_id: {target_id}")
        seen.add(target_id)
        status = item.get("status")
        if not isinstance(status, str) or not status.strip():
            raise ExecutionTopologyError(f"execution target {target_id} status is invalid")
        formal = item.get("formal_execution")
        if not isinstance(formal, bool):
            raise ExecutionTopologyError(
                f"execution target {target_id} formal_execution must be a boolean"
            )
        host_id = (
            None
            if "host_id" not in item
            else _token(item.get("host_id"), f"execution target {target_id} host_id")
        )
        license_pool_id = (
            _DEFAULT_LICENSE_POOL
            if "license_pool_id" not in item
            else _token(
                item.get("license_pool_id"),
                f"execution target {target_id} license_pool_id",
            )
        )
        records.append(
            {
                "target_id": target_id,
                "status": status.strip(),
                "formal_execution": formal,
                "host_id": host_id,
                "license_pool_id": license_pool_id,
            }
        )

    host_groups: dict[str, list[str]] = {}
    license_groups: dict[str, list[str]] = {}
    for target in records:
        if target["host_id"] is not None:
            host_groups.setdefault(target["host_id"], []).append(target["target_id"])
        license_groups.setdefault(target["license_pool_id"], []).append(target["target_id"])

    # Only active formal targets participate in scheduling.  A missing host is
    # still retained as None and is never inferred to be a private host.
    formal_target_ids = [
        target["target_id"]
        for target in records
        if target["status"] == "active" and target["formal_execution"] is True
    ]
    return {
        "targets": records,
        "host_groups": host_groups,
        "license_pool_groups": license_groups,
        "formal_target_ids": formal_target_ids,
    }


def check_formal_target_readiness(
    topology: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a serializable fail-closed readiness result.

    A one-target formal set remains compatible with existing projects and does
    not require a host declaration.  A multi-target set requires every target
    to declare one.
    """
    if isinstance(topology, Mapping):
        records = topology.get("targets", ())
        formal_ids = topology.get("formal_target_ids")
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            raise ExecutionTopologyError("topology targets must be an array")
        if not isinstance(formal_ids, Sequence) or isinstance(formal_ids, (str, bytes)):
            formal_ids = [
                item.get("target_id")
                for item in records
                if isinstance(item, Mapping) and item.get("formal_execution") is True
            ]
    else:
        records = topology
        formal_ids = [
            item.get("target_id")
            for item in records
            if isinstance(item, Mapping) and item.get("formal_execution") is True
        ]
    formal_ids = [str(value) for value in formal_ids]
    by_id = {
        str(item.get("target_id")): item
        for item in records
        if isinstance(item, Mapping)
    }
    missing = [
        target_id
        for target_id in formal_ids
        if not by_id.get(target_id, {}).get("host_id")
    ] if len(formal_ids) > 1 else []
    ready = not missing
    result: dict[str, Any] = {
        "ready": ready,
        "status": "ready" if ready else "blocked",
        "formal_target_ids": formal_ids,
        "missing_host_ids": missing,
    }
    if missing:
        result["error"] = (
            "multi-target scheduling requires host_id for formal target(s): "
            + ", ".join(missing)
        )
    else:
        result["error"] = None
    return result


def ensure_formal_targets_ready(
    topology: Mapping[str, Any] | Sequence[Mapping[str, Any]],
) -> None:
    """Raise a controlled error when formal multi-target scheduling is unsafe."""
    result = check_formal_target_readiness(topology)
    if not result["ready"]:
        raise ExecutionTopologyError(str(result["error"]))


__all__ = [
    "ExecutionTopologyError",
    "ProjectFileTargetCatalog",
    "parse_execution_topology",
    "check_formal_target_readiness",
    "ensure_formal_targets_ready",
]
