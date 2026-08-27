"""Thin HTTP mutation request assembly for the status server."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from control_plane.core.evaluation_contracts import (
    ContractError,
    make_candidate,
    make_evaluation_request,
    make_problem_definition,
)

_BUILDERS: dict[str, Callable[..., dict[str, Any]]] = {
    "problem": make_problem_definition,
    "candidate": make_candidate,
    "evaluation_request": make_evaluation_request,
}


def _object(body: Any, label: str = "request body") -> dict[str, Any]:
    if not isinstance(body, Mapping):
        raise ContractError(f"{label} must be an object")
    return dict(body)


def build_contract(body: Any) -> dict[str, Any]:
    source = _object(body)
    kind = source.get("kind")
    if kind not in _BUILDERS:
        raise ContractError("kind must be problem, candidate, or evaluation_request")
    spec = source.get("spec")
    if not isinstance(spec, Mapping):
        raise ContractError("spec must be an object")
    try:
        contract = _BUILDERS[kind](**dict(spec))
    except TypeError as exc:
        raise ContractError("invalid contract fields") from exc
    return {"contract": contract}


def register_problem(middleware: Any, body: Any) -> dict[str, Any]:
    return middleware.register_problem(_object(body, "ProblemDefinition"))


def create_study(middleware: Any, body: Any) -> dict[str, Any]:
    source = _object(body)
    allowed = {
        "study_id", "problem_id", "problem_revision", "metadata",
        "algorithm_run_id", "artifact_refs", "automation_profile",
    }
    unknown = set(source) - allowed
    if unknown:
        raise ContractError(f"unknown study fields: {', '.join(sorted(unknown))}")
    required = {"study_id", "problem_id", "problem_revision"}
    if not required.issubset(source):
        raise ContractError("study_id, problem_id, and problem_revision are required")
    return middleware.create_study(**source)


def submit_evaluation(middleware: Any, body: Any) -> dict[str, Any]:
    source = _object(body)
    allowed = {"candidate", "request", "study_id"}
    unknown = set(source) - allowed
    if unknown:
        raise ContractError(f"unknown evaluation fields: {', '.join(sorted(unknown))}")
    if "candidate" not in source or "request" not in source:
        raise ContractError("candidate and request are required")
    study_id = source.get("study_id")
    return middleware.submit(source["candidate"], source["request"], study_id=study_id)
