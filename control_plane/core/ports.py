"""Dependency-inversion ports for project governance and evaluation services.

These are deliberately storage/materialization boundaries.  Runtime scheduling
and simulation gateway protocols remain defined by their owning modules.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol


class ControlStore(Protocol):
    """Read the governed project control state (PROJECT_STATE.json)."""

    def read_project_state(self, project_root: Path | str) -> Mapping[str, Any]: ...


class ArtifactStore(Protocol):
    """Resolve and parse a registered artifact record by stable identity."""

    def read_artifact(
        self, project_root: Path | str, artifact_id: str
    ) -> Mapping[str, Any]: ...


class TargetCatalog(Protocol):
    """Read the governed execution-target catalog without mutating it."""

    def read_targets(self, project_root: Path | str) -> Sequence[Mapping[str, Any]]: ...


class ResourceMonitor(Protocol):
    """Provide locked, read-only resource snapshots for scheduling."""

    def locked_snapshot(self, target_id: str) -> AbstractContextManager[dict[str, Any]]: ...


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
