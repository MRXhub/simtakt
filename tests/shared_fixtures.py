"""Shared fixtures for tests that persist problem definitions."""
from __future__ import annotations

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
