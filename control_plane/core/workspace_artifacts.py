"""Read-only resolution of governed artifacts stored in this workspace."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


_SAFE_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REVISION = re.compile(r"^sha256:[0-9a-f]{64}$")
_FORBIDDEN_COMPONENTS = frozenset({"archive", "legacy", "tmp"})
_SUPPORTED_HASH_SCOPES = frozenset({"file", "package-manifest"})


class WorkspaceArtifactError(ValueError):
    """Raised when a workspace artifact cannot be resolved safely."""


@dataclass(frozen=True)
class ResolvedWorkspaceArtifact:
    artifact_id: str
    kind: str
    revision: str
    hash_scope: str
    path: Path


def confined_workspace_path(
    project_root: Path,
    value: Path | str,
    *,
    label: str = "workspace path",
) -> Path:
    """Resolve one path below the project root and outside forbidden storage."""

    root = Path(project_root).resolve()
    path = (root / value).resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise WorkspaceArtifactError(
            f"{label} must stay below the project root"
        ) from exc
    if {part.casefold() for part in relative.parts} & _FORBIDDEN_COMPONENTS:
        raise WorkspaceArtifactError(
            f"{label} cannot use archive, legacy, or tmp storage"
        )
    return path


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceArtifactError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise WorkspaceArtifactError(f"{label} must be a JSON object")
    return value


def _workspace_location(
    project_root: Path,
    revision_entry: Mapping[str, Any],
    *,
    current: bool,
) -> Path:
    location_values = revision_entry.get("locations")
    if not isinstance(location_values, list):
        raise WorkspaceArtifactError("artifact locations are invalid")
    locations = [
        item
        for item in location_values
        if isinstance(item, Mapping)
        and item.get("storage") == "workspace"
        and item.get("availability") != "retired"
        and (not current or item.get("role") == "primary")
    ]
    if len(locations) != 1:
        qualifier = "current primary" if current else "exact revision"
        raise WorkspaceArtifactError(
            f"artifact has no unique {qualifier} workspace path"
        )
    location = locations[0].get("path")
    if not isinstance(location, str) or not location.strip():
        raise WorkspaceArtifactError("artifact workspace path is invalid")
    return confined_workspace_path(
        project_root,
        location,
        label="artifact path",
    )


def resolve_workspace_artifact(
    project_root: Path,
    artifact_id: str,
    *,
    revision: str | None = None,
    expected_kind: str | None = None,
) -> ResolvedWorkspaceArtifact:
    """Resolve and hash-check an active artifact's current or exact revision."""

    if not _SAFE_ARTIFACT_ID.fullmatch(str(artifact_id)):
        raise WorkspaceArtifactError("artifact_id is invalid")
    root = Path(project_root).resolve()
    shard_path = confined_workspace_path(
        root,
        Path("records") / "artifacts" / f"{artifact_id}.json",
        label="artifact shard",
    )
    shard = _read_json_object(shard_path, "artifact shard")
    artifact = shard.get("artifact")
    if (
        shard.get("schema_version") != 1
        or shard.get("record_kind") != "artifact-catalog-shard"
        or not isinstance(artifact, Mapping)
        or artifact.get("artifact_id") != artifact_id
        or artifact.get("status") != "active"
    ):
        raise WorkspaceArtifactError(
            "artifact is not one active registry record"
        )
    kind = artifact.get("kind")
    if not isinstance(kind, str) or not kind:
        raise WorkspaceArtifactError("artifact kind is invalid")
    if expected_kind is not None and kind != expected_kind:
        raise WorkspaceArtifactError(
            f"artifact is not registered as {expected_kind}"
        )

    latest_revision = str(artifact.get("latest_revision", "")).lower()
    selected_revision = (
        latest_revision if revision is None else str(revision).lower()
    )
    if not _REVISION.fullmatch(selected_revision):
        raise WorkspaceArtifactError("artifact revision is invalid")
    revision_values = artifact.get("revisions")
    if not isinstance(revision_values, list):
        raise WorkspaceArtifactError("artifact revisions are invalid")
    revisions = [
        item
        for item in revision_values
        if isinstance(item, Mapping)
        and str(item.get("revision", "")).lower() == selected_revision
    ]
    if len(revisions) != 1:
        raise WorkspaceArtifactError(
            "artifact revision does not resolve exactly once"
        )
    hash_scope = revisions[0].get("hash_scope")
    if hash_scope not in _SUPPORTED_HASH_SCOPES:
        raise WorkspaceArtifactError("artifact hash scope is unsupported")
    path = _workspace_location(
        root,
        revisions[0],
        current=selected_revision == latest_revision,
    )

    hash_path = path
    if hash_scope == "package-manifest":
        hash_path = confined_workspace_path(
            root,
            path / "manifest.json",
            label="artifact manifest",
        )
    try:
        actual_revision = (
            "sha256:" + hashlib.sha256(hash_path.read_bytes()).hexdigest()
        )
    except OSError as exc:
        missing = (
            "artifact manifest"
            if hash_scope == "package-manifest"
            else "artifact file"
        )
        raise WorkspaceArtifactError(f"{missing} is missing") from exc
    if actual_revision != selected_revision:
        raise WorkspaceArtifactError(
            "artifact bytes do not match the registry revision"
        )
    return ResolvedWorkspaceArtifact(
        artifact_id=artifact_id,
        kind=kind,
        revision=selected_revision,
        hash_scope=str(hash_scope),
        path=path,
    )
