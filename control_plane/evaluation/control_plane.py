"""Project-wide evaluation control-plane location."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from control_plane.core.evaluation_contracts import ACTIVE_ATTEMPT_STATES


CONTROL_PLANE_DATABASE_RELATIVE_PATH = Path(
    "data/outputs/evaluation-middleware/control.sqlite3"
)


class ControlPlanePathError(ValueError):
    """Raised when the project control-plane path is not safe to use."""


class ControlPlaneCutoverError(RuntimeError):
    """Raised when a legacy local queue still owns nonterminal work."""


def resolve_control_plane_database(project_root: Path | str) -> Path:
    """Resolve the fixed project database and create its parent directory."""

    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ControlPlanePathError(
            "project root must be an existing directory"
        )
    database = (root / CONTROL_PLANE_DATABASE_RELATIVE_PATH).resolve()
    try:
        database.relative_to(root)
    except ValueError as exc:
        raise ControlPlanePathError(
            "control-plane database must stay below the project root"
        ) from exc
    database.parent.mkdir(parents=True, exist_ok=True)
    return database


def legacy_control_plane_activity(
    project_root: Path | str,
) -> list[dict[str, Any]]:
    """Read legacy middleware databases without migrating or writing them."""

    root = Path(project_root).expanduser().resolve()
    global_database = resolve_control_plane_database(root)
    control_root = global_database.parent
    active_statuses = tuple(sorted(ACTIVE_ATTEMPT_STATES))
    activity: list[dict[str, Any]] = []
    for database in sorted(control_root.rglob("control.sqlite3")):
        resolved = database.resolve()
        if resolved == global_database:
            continue
        try:
            resolved.relative_to(control_root)
        except ValueError as exc:
            raise ControlPlaneCutoverError(
                "legacy control database escapes the middleware output root"
            ) from exc
        try:
            connection = sqlite3.connect(
                f"{resolved.as_uri()}?mode=ro", uri=True, timeout=1
            )
            try:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                if "attempts" not in tables:
                    continue
                placeholders = ",".join("?" for _ in active_statuses)
                rows = connection.execute(
                    "SELECT status, COUNT(*) FROM attempts "
                    f"WHERE status IN ({placeholders}) GROUP BY status",
                    active_statuses,
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise ControlPlaneCutoverError(
                f"cannot audit legacy control database: {resolved.relative_to(root)}"
            ) from exc
        counts = {str(status): int(count) for status, count in rows}
        if counts:
            activity.append(
                {
                    "database": resolved.relative_to(root).as_posix(),
                    "active_attempts": sum(counts.values()),
                    "status_counts": dict(sorted(counts.items())),
                }
            )
    return activity


def assert_legacy_control_planes_drained(project_root: Path | str) -> None:
    """Fail closed until every pre-cutover queue has reached terminal state."""

    activity = legacy_control_plane_activity(project_root)
    if activity:
        summary = "; ".join(
            f"{item['database']}={item['status_counts']}" for item in activity
        )
        raise ControlPlaneCutoverError(
            "project-wide dispatch is blocked while legacy queues are active: "
            + summary
        )
