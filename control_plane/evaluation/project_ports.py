"""Project-file implementations of evaluation storage ports."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from control_plane.core.ports import ControlStore


class ProjectFileControlStore:
    """Project-file control store; preparation no longer depends on PROJECT_STATE."""

    def read_project_state(self, project_root: Path | str) -> dict[str, Any]:
        value, _ = self.read_project_state_with_revision(project_root)
        return value

    def read_project_state_with_revision(
        self, project_root: Path | str
    ) -> tuple[dict[str, Any], str]:
        path = Path(project_root).resolve() / "project" / "PROJECT_STATE.json"
        try:
            raw = path.read_bytes()
            value = json.loads(raw.decode("utf-8-sig"))
        except FileNotFoundError:
            return {}, "sha256:" + "0" * 64
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("cannot read PROJECT_STATE") from exc
        if not isinstance(value, dict):
            raise ValueError("PROJECT_STATE must be a JSON object")
        return value, "sha256:" + hashlib.sha256(raw).hexdigest()


__all__ = ["ProjectFileControlStore"]
