"""Dependency-inversion ports for project governance and evaluation services.

These are deliberately storage/materialization boundaries.  Runtime scheduling
and simulation gateway protocols remain defined by their owning modules.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol


class ControlStore(Protocol):
    """Read the governed project control state (PROJECT_STATE.json)."""

    def read_project_state(self, project_root: Path | str) -> Mapping[str, Any]: ...

    def read_project_state_with_revision(
        self, project_root: Path | str
    ) -> tuple[Mapping[str, Any], str]: ...


class ArtifactStore(Protocol):
    """Resolve and parse a registered artifact record by stable identity."""

    def read_artifact(
        self, project_root: Path | str, artifact_id: str
    ) -> Mapping[str, Any]: ...


class TargetCatalog(Protocol):
    """Read the governed execution-target catalog without mutating it."""

    def read_targets(self, project_root: Path | str) -> Sequence[Mapping[str, Any]]: ...

    def read_targets_with_revision(
        self, project_root: Path | str
    ) -> tuple[Sequence[Mapping[str, Any]], str]: ...


class ResourceMonitor(Protocol):
    """Provide locked resource snapshots and record scheduling decisions."""

    def locked_snapshot(
        self, target_id: str
    ) -> AbstractContextManager[dict[str, Any]]: ...

    def record_decision(
        self,
        decision: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
        active_allocations: Sequence[Mapping[str, Any]],
        resource_snapshot: Mapping[str, Any],
        *,
        scheduling_policy: Mapping[str, Any] | None = None,
        decision_time: datetime | str | None = None,
        capacity_envelope: Mapping[str, Any] | None = None,
        capacity_profile_snapshot: Mapping[str, Any] | None = None,
        capacity_scope: set[str] | Sequence[str] | None = None,
        task_classes: Sequence[Mapping[str, Any]] = (),
        overrides: Sequence[Mapping[str, Any]] = (),
        scheduling_policy_provenance: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[str, Path]:
        """Persist a scheduling decision receipt and return (artifact_id, artifact_path).

        Note on backward compatibility:
        Dispatchers support older/simplified monitors by falling back to a minimal
        keyword signature (scheduling_policy, decision_time, capacity_envelope)
        if newer keyword arguments trigger a TypeError containing 'unexpected keyword'.
        """
        ...

    def locked_dispatch(self) -> AbstractContextManager[Any]:
        """Acquire a cross-target lock during multi-target candidate dispatch.

        Optional:
        Single-target dispatch falls back to ``nullcontext()`` if this method
        is not provided. Multi-target dispatch strictly requires this method;
        omitting it in a multi-target environment will cause
        ``PreparedExecutionDispatcher.dispatch_once`` to raise ``DispatchError``.
        """
        ...


class ProjectMaterializer(Protocol):
    """Materialize a validated task/project execution input."""

    def materialize(
        self, project_root: Path | str, task: Mapping[str, Any], **kwargs: Any
    ) -> Mapping[str, Any]: ...


__all__ = [
    "ControlStore",
    "ArtifactStore",
    "TargetCatalog",
    "ResourceMonitor",
    "ProjectMaterializer",
]
