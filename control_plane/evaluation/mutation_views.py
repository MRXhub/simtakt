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

from control_plane.simulation.deck_parameters import parse_deck_parameters
from control_plane.evaluation.parameter_schema import validate_parameters
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
def parse_deck(body: Any) -> dict[str, Any]:
    source = _object(body, "parse request")
    if "deck_text" not in source:
        raise ContractError("deck_text is required")
    deck_text = source.get("deck_text")
    if not isinstance(deck_text, str):
        raise ContractError("deck_text must be a string")
    return parse_deck_parameters(deck_text)


def register_schema(middleware: Any, body: Any) -> dict[str, Any]:
    source = _object(body, "ParameterSchema")
    record = middleware.register_schema(source)
    return {"revision": record["revision"]}


def validate_candidate_parameters(middleware: Any, body: Any) -> dict[str, Any]:
    source = _object(body, "candidate validation request")
    if "schema_revision" not in source or "parameters" not in source:
        raise ContractError("schema_revision and parameters are required")
    schema_revision = str(source["schema_revision"]).strip().lower()
    raw_params = source["parameters"]
    if not isinstance(raw_params, Mapping):
        raise ContractError("parameters must be an object")
    schema_doc = middleware.get_schema(schema_revision)
    schema_data = schema_doc.get("schema", schema_doc)
    return validate_parameters(schema_data, dict(raw_params))
