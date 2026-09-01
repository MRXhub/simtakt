"""Project-governed policy for bounded preparation admission."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from control_plane.core.ports import ControlStore
from control_plane.core.workspace_artifacts import (
    WorkspaceArtifactError,
    resolve_workspace_artifact,
)


_REVISION = re.compile(r"^sha256:[0-9a-f]{64}$")
_PRIORITY = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_POLICY_ARTIFACT = re.compile(
    r"^configuration\.project-scheduling-policy\.[a-z0-9][a-z0-9._-]{0,79}$"
)
_POLICY_FIELDS = frozenset(
    {
        "schema_version",
        "policy_kind",
        "status",
        "capacity_envelope",
        "priority_order",
        "default_priority",
        "aging_quantum_seconds",
        "preparation_claim_seconds",
        "option_policy",
    }
)
_REQUIRED_CAPACITY_FIELDS = frozenset(
    {
        "processors",
        "memory_bytes",
        "license_sessions",
        "baseline_processors",
        "baseline_memory_bytes",
    }
)
_CAPACITY_FIELDS = _REQUIRED_CAPACITY_FIELDS | {"license_reserve"}
_STATE_REFERENCE_FIELDS = frozenset({"artifact_id", "revision", "status"})
_OPTION_POLICIES = frozenset({"throughput", "latency"})
_POLICY_SEAL = object()


class SchedulingPolicyError(ValueError):
    """Raised when project scheduling policy authority is inconsistent."""


class SchedulingPolicyBlocked(SchedulingPolicyError):
    """Raised for an expected fail-closed policy configuration state."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SchedulingPolicyError(f"{label} must be a positive integer")
    return value


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchedulingPolicyError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise SchedulingPolicyError(f"{label} must be a JSON object")
    return value


def _read_state(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SchedulingPolicyError("cannot read PROJECT_STATE") from exc
    if not isinstance(value, dict):
        raise SchedulingPolicyError("PROJECT_STATE must be a JSON object")
    return value, "sha256:" + hashlib.sha256(raw).hexdigest()


def _priority_order(value: Any) -> list[str]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, Sequence
    ):
        raise SchedulingPolicyError("priority_order must be an array")
    priorities = [str(item).strip().lower() for item in value]
    if (
        not priorities
        or any(not _PRIORITY.fullmatch(item) for item in priorities)
        or len(priorities) != len(set(priorities))
    ):
        raise SchedulingPolicyError(
            "priority_order must contain unique normalized priorities"
        )
    return priorities


def validate_scheduling_policy(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and copy the complete immutable SchedulingPolicy contract."""

    if not isinstance(value, Mapping) or set(value) != _POLICY_FIELDS:
        raise SchedulingPolicyError(
            "SchedulingPolicy contains missing or unknown fields"
        )
    capacity = value.get("capacity_envelope")
    if not isinstance(capacity, Mapping) or set(capacity) not in (
        _REQUIRED_CAPACITY_FIELDS,
        _CAPACITY_FIELDS,
    ):
        raise SchedulingPolicyError(
            "SchedulingPolicy capacity_envelope is invalid"
        )
    normalized_capacity = {
        field: _positive_integer(
            capacity.get(field), f"capacity_envelope.{field}"
        )
        for field in sorted(_REQUIRED_CAPACITY_FIELDS)
    }
    if "license_reserve" in capacity:
        reserve = capacity["license_reserve"]
        if isinstance(reserve, bool) or not isinstance(reserve, int) or reserve < 0:
            raise SchedulingPolicyError(
                "capacity_envelope.license_reserve must be a nonnegative integer"
            )
        if reserve >= normalized_capacity["license_sessions"]:
            raise SchedulingPolicyError(
                "capacity_envelope.license_reserve must be less than license_sessions"
            )
        normalized_capacity["license_reserve"] = reserve
    if (
        normalized_capacity["baseline_processors"]
        > normalized_capacity["processors"]
        or normalized_capacity["baseline_memory_bytes"]
        > normalized_capacity["memory_bytes"]
    ):
        raise SchedulingPolicyError(
            "baseline resources exceed the capacity envelope"
        )
    priorities = _priority_order(value.get("priority_order"))
    default_priority = str(value.get("default_priority", "")).strip().lower()
    if default_priority not in priorities:
        raise SchedulingPolicyError(
            "default_priority must belong to priority_order"
        )
    option_policy = str(value.get("option_policy", "")).strip().lower()
    if option_policy not in _OPTION_POLICIES:
        raise SchedulingPolicyError(
            "option_policy must be throughput or latency"
        )
    normalized = {
        "schema_version": value.get("schema_version"),
        "policy_kind": value.get("policy_kind"),
        "status": value.get("status"),
        "capacity_envelope": normalized_capacity,
        "priority_order": priorities,
        "default_priority": default_priority,
        "aging_quantum_seconds": _positive_integer(
            value.get("aging_quantum_seconds"), "aging_quantum_seconds"
        ),
        "preparation_claim_seconds": _positive_integer(
            value.get("preparation_claim_seconds"),
            "preparation_claim_seconds",
        ),
        "option_policy": option_policy,
    }
    if (
        normalized["schema_version"] != 1
        or normalized["policy_kind"] != "project-scheduling-policy"
        or normalized["status"] != "active"
        or dict(value) != normalized
    ):
        raise SchedulingPolicyError("SchedulingPolicy is invalid")
    return json.loads(
        json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def capacity_slots(policy: Mapping[str, Any]) -> int:
    """Return conservative baseline concurrency from all three capacities."""

    normalized = validate_scheduling_policy(policy)
    capacity = normalized["capacity_envelope"]
    return min(
        capacity["processors"] // capacity["baseline_processors"],
        capacity["memory_bytes"] // capacity["baseline_memory_bytes"],
        capacity["license_sessions"],
    )


def preparation_window_limit(policy: Mapping[str, Any]) -> int:
    """Return ceil(1.5 * baseline capacity) using integer arithmetic."""

    slots = capacity_slots(policy)
    return (3 * slots + 1) // 2


class GovernedSchedulingPolicy:
    """Opaque exact policy authority returned only by the project resolver."""

    __slots__ = ("_canonical_policy", "_project_root", "_provenance", "_seal")

    def __init__(
        self,
        policy: Mapping[str, Any],
        *,
        project_root: Path,
        artifact_id: str,
        artifact_revision: str,
        project_state_revision: str,
        _seal: object,
    ) -> None:
        if _seal is not _POLICY_SEAL:
            raise SchedulingPolicyError(
                "GovernedSchedulingPolicy must come from the project resolver"
            )
        normalized = validate_scheduling_policy(policy)
        self._canonical_policy = json.dumps(
            normalized,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._project_root = Path(project_root).resolve()
        self._provenance = {
            "artifact_id": artifact_id,
            "revision": artifact_revision,
            "project_state_revision": project_state_revision,
        }
        self._seal = _seal

    def as_mapping(self) -> dict[str, Any]:
        return json.loads(self._canonical_policy)

    def provenance(self) -> dict[str, str]:
        return dict(self._provenance)

    def is_attested_for(self, project_root: Path | str) -> bool:
        return (
            self._seal is _POLICY_SEAL
            and self._project_root == Path(project_root).resolve()
        )

    @property
    def capacity_slots(self) -> int:
        return capacity_slots(self.as_mapping())

    @property
    def window_limit(self) -> int:
        slots = self.capacity_slots
        return (3 * slots + 1) // 2

    @property
    def priority_order(self) -> tuple[str, ...]:
        return tuple(self.as_mapping()["priority_order"])

    @property
    def default_priority(self) -> str:
        return str(self.as_mapping()["default_priority"])

    @property
    def aging_quantum_seconds(self) -> int:
        return int(self.as_mapping()["aging_quantum_seconds"])

    @property
    def preparation_claim_seconds(self) -> int:
        return int(self.as_mapping()["preparation_claim_seconds"])

    @property
    def option_policy(self) -> str:
        return str(self.as_mapping()["option_policy"])

    @property
    def license_reserve(self) -> int:
        """Platform license sessions reserved outside the dispatcher ledger."""
        return int(self.as_mapping()["capacity_envelope"].get("license_reserve", 0))


def resolve_governed_scheduling_policy(
    project_root: Path | str,
    *,
    control_store: "ControlStore | None" = None,
) -> GovernedSchedulingPolicy:
    root = Path(project_root).resolve()
    store = control_store
    if store is None:
        from control_plane.evaluation.project_ports import ProjectFileControlStore
        store = ProjectFileControlStore()
    try:
        state, state_revision = store.read_project_state_with_revision(root)
    except (OSError, ValueError) as exc:
        raise SchedulingPolicyError("cannot read PROJECT_STATE") from exc
    if state.get("schema_version") != 2 or state.get("status") != "active":
        raise SchedulingPolicyError(
            "PROJECT_STATE must be active schema version 2"
        )
    reference = state.get("scheduling_policy")
    if reference is None:
        raise SchedulingPolicyBlocked("scheduling-policy-not-configured")
    if (
        not isinstance(reference, Mapping)
        or set(reference) != _STATE_REFERENCE_FIELDS
    ):
        raise SchedulingPolicyError(
            "PROJECT_STATE scheduling_policy reference is invalid"
        )
    artifact_id = str(reference.get("artifact_id", ""))
    revision = str(reference.get("revision", "")).lower()
    if (
        not _POLICY_ARTIFACT.fullmatch(artifact_id)
        or not _REVISION.fullmatch(revision)
        or reference.get("revision") != revision
        or reference.get("status") != "active"
    ):
        raise SchedulingPolicyError(
            "PROJECT_STATE scheduling_policy reference is invalid"
        )
    try:
        resolved = resolve_workspace_artifact(
            root,
            artifact_id,
            revision=revision,
            expected_kind="configuration",
        )
    except WorkspaceArtifactError as exc:
        raise SchedulingPolicyError(
            "SchedulingPolicy is not an exact active artifact"
        ) from exc
    if resolved.hash_scope != "file":
        raise SchedulingPolicyError("SchedulingPolicy must use file hash scope")
    policy = validate_scheduling_policy(
        _read_json(resolved.path, "SchedulingPolicy")
    )
    return GovernedSchedulingPolicy(
        policy,
        project_root=root,
        artifact_id=artifact_id,
        artifact_revision=revision,
        project_state_revision=state_revision,
        _seal=_POLICY_SEAL,
    )
