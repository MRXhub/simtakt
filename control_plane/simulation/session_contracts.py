"""Versioned boundary records for one proxy-managed simulation session."""

from __future__ import annotations

import hashlib
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from control_plane.core.evaluation_contracts import ContractError, canonical_json


SESSION_CONTRACT_VERSION = 1
SESSION_RESULT_STATES = frozenset({"completed", "exhausted", "indeterminate"})
SOLVER_RUN_STATES = frozenset({"completed", "failed", "indeterminate"})

_REVISION = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_PLAN_ID = re.compile(r"^simulation-plan:sha256:[0-9a-f]{64}$")
_RUN_RECORD_ID = re.compile(r"^solver-run-record:sha256:[0-9a-f]{64}$")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{label} must be an object")
    return dict(value)


def _text(value: Any, label: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text or not _TOKEN.fullmatch(text):
        raise ContractError(f"{label} must be a non-empty stable token")
    return text


def _revision(value: Any, label: str) -> str:
    revision = str(value).strip().lower()
    if not _REVISION.fullmatch(revision):
        raise ContractError(f"{label} must be sha256:<64 lowercase hex characters>")
    return revision


def _identity(value: Any, prefix: str, label: str) -> str:
    text = str(value).strip().lower()
    expected = f"{prefix}:"
    if not text.startswith(expected):
        raise ContractError(f"{label} must start with {expected}")
    try:
        parsed = uuid.UUID(text[len(expected) :])
    except ValueError as exc:
        raise ContractError(f"{label} must contain a UUID") from exc
    canonical = expected + str(parsed)
    if text != canonical:
        raise ContractError(f"{label} must use canonical lowercase UUID form")
    return canonical


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractError(f"{label} must be a positive integer")
    return value


def _positive_metric(value: Any, label: str, *, integer: bool = False) -> float | int | None:
    """Validate an optional measured duration/resource metric."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{label} must be a positive finite number or null")
    if not math.isfinite(float(value)) or float(value) <= 0:
        raise ContractError(f"{label} must be a positive finite number or null")
    if integer:
        if not isinstance(value, int):
            raise ContractError(f"{label} must be a positive integer or null")
        return value
    return float(value)


def _tokens(value: Any, label: str, *, allow_empty: bool = True) -> list[str]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ContractError(f"{label} must be an array")
    result = [_text(item, f"{label}[{index}]") for index, item in enumerate(value)]
    if not allow_empty and not result:
        raise ContractError(f"{label} must not be empty")
    if len(result) != len(set(result)):
        raise ContractError(f"{label} must not contain duplicates")
    return sorted(result)


def _artifact_ref(artifact_id: Any, revision: Any, label: str) -> dict[str, str]:
    return {
        "artifact_id": _text(artifact_id, f"{label}.artifact_id"),
        "revision": _revision(revision, f"{label}.revision"),
    }


def _hash_identity(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}:sha256:{digest}"


def normalize_session_ref(value: Any) -> str:
    """Validate the stable identity used to reconcile a remote session."""

    return _text(value, "session_ref")


def normalize_plan_id(value: Any) -> str:
    """Validate a content-addressed simulation session plan identity."""

    plan_id = str(value).strip().lower()
    if not _PLAN_ID.fullmatch(plan_id):
        raise ContractError("execution_plan_id must be a SimulationSessionPlan identity")
    return plan_id


def normalize_artifact_id(value: Any, label: str = "artifact_id") -> str:
    """Validate one stable artifact identity used by a session receipt."""

    return _text(value, label)


def make_simulation_session_plan(
    *,
    attempt_id: str,
    evaluation_id: str,
    candidate_id: str,
    simulation_proxy: str,
    recovery_profile_revision: str,
    base_package_artifact_id: str,
    base_package_revision: str,
    target_id: str,
    task_id: str | None = None,
    authorization_id: str | None = None,
    authorization_revision: str | None = None,
    requested_processors: int,
    command_timeout_seconds: int,
    max_solver_runs: int,
    max_wall_seconds: int,
) -> dict[str, Any]:
    """Create the immutable instruction record accepted by a simulation proxy."""

    candidate = str(candidate_id).strip().lower()
    if not re.fullmatch(r"candidate:sha256:[0-9a-f]{64}", candidate):
        raise ContractError("candidate_id must be a content-addressed Candidate identity")
    authorization = (
        None
        if authorization_id is None and authorization_revision is None
        else _artifact_ref(authorization_id, authorization_revision, "authorization")
    )
    if (authorization_id is None) != (authorization_revision is None):
        raise ContractError("authorization artifact_id and revision must be supplied together")
    body = {
        "contract_version": SESSION_CONTRACT_VERSION,
        "attempt_id": _identity(attempt_id, "attempt", "attempt_id"),
        "evaluation_id": _identity(evaluation_id, "evaluation", "evaluation_id"),
        "candidate_id": candidate,
        "simulation_proxy": _text(simulation_proxy, "simulation_proxy"),
        "recovery_profile_revision": _revision(
            recovery_profile_revision, "recovery_profile_revision"
        ),
        "base_package": _artifact_ref(
            base_package_artifact_id, base_package_revision, "base_package"
        ),
        "task_id": None if task_id is None else _text(task_id, "task_id"),
        "target_id": _text(target_id, "target_id"),
        "authorization": authorization,
        "resources": {
            "requested_processors": _positive_integer(
                requested_processors, "requested_processors"
            )
        },
        "budget": {
            "command_timeout_seconds": _positive_integer(
                command_timeout_seconds, "command_timeout_seconds"
            ),
            "max_solver_runs": _positive_integer(max_solver_runs, "max_solver_runs"),
            "max_wall_seconds": _positive_integer(max_wall_seconds, "max_wall_seconds"),
        },
    }
    return {**body, "plan_id": _hash_identity("simulation-plan", body)}


def validate_simulation_session_plan(value: Any) -> dict[str, Any]:
    source = _object(value, "SimulationSessionPlan")
    base_package = _object(source.get("base_package"), "base_package")
    authorization = source.get("authorization")
    if authorization is not None and not isinstance(authorization, Mapping):
        raise ContractError("authorization must be an object or null")
    resources = _object(source.get("resources"), "resources")
    budget = _object(source.get("budget"), "budget")
    expected = make_simulation_session_plan(
        attempt_id=source.get("attempt_id"),
        evaluation_id=source.get("evaluation_id"),
        candidate_id=source.get("candidate_id"),
        simulation_proxy=source.get("simulation_proxy"),
        recovery_profile_revision=source.get("recovery_profile_revision"),
        base_package_artifact_id=base_package.get("artifact_id"),
        base_package_revision=base_package.get("revision"),
        task_id=source.get("task_id"),
        target_id=source.get("target_id"),
        authorization_id=None if authorization is None else authorization.get("artifact_id"),
        authorization_revision=None if authorization is None else authorization.get("revision"),
        requested_processors=resources.get("requested_processors"),
        command_timeout_seconds=budget.get("command_timeout_seconds"),
        max_solver_runs=budget.get("max_solver_runs"),
        max_wall_seconds=budget.get("max_wall_seconds"),
    )
    if source.get("contract_version") != SESSION_CONTRACT_VERSION:
        raise ContractError(
            f"SimulationSessionPlan contract_version must be {SESSION_CONTRACT_VERSION}"
        )
    if source.get("plan_id") != expected["plan_id"]:
        raise ContractError("SimulationSessionPlan plan_id does not match its content")
    if set(source) != set(expected):
        raise ContractError("SimulationSessionPlan contains missing or unknown fields")
    return expected


def make_solver_run_record(
    *,
    plan_id: str,
    sequence: int,
    run_id: str,
    package_artifact_id: str,
    package_revision: str,
    numerical_profile_revision: str,
    action: str,
    status: str,
    cause: str | None = None,
    parent_run_id: str | None = None,
    exit_code: int | None = None,
    artifact_ids: Sequence[str] = (),
    wall_seconds: float | None = None,
    cpu_seconds: float | None = None,
    peak_rss_bytes: int | None = None,
) -> dict[str, Any]:
    """Record one governed solver invocation inside a simulation session."""
    normalized_plan = normalize_plan_id(plan_id)
    outcome = str(status).strip().lower()
    if outcome not in SOLVER_RUN_STATES:
        raise ContractError("solver run status must be completed, failed, or indeterminate")
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        raise ContractError("exit_code must be an integer or null")
    if outcome == "completed" and exit_code != 0:
        raise ContractError("completed solver run requires exit_code=0")
    if outcome == "indeterminate" and exit_code is not None:
        raise ContractError("indeterminate solver run cannot claim an exit code")
    artifacts = _tokens(
        artifact_ids, "artifact_ids", allow_empty=outcome != "completed"
    )
    body = {
        "contract_version": SESSION_CONTRACT_VERSION,
        "plan_id": normalized_plan,
        "sequence": _positive_integer(sequence, "sequence"),
        "run_id": _text(run_id, "run_id"),
        "parent_run_id": (
            None if parent_run_id is None else _text(parent_run_id, "parent_run_id")
        ),
        "action": _text(action, "action"),
        "cause": None if cause is None else _text(cause, "cause"),
        "package": _artifact_ref(package_artifact_id, package_revision, "package"),
        "numerical_profile_revision": _revision(
            numerical_profile_revision, "numerical_profile_revision"
        ),
        "status": outcome,
        "exit_code": exit_code,
        "artifact_ids": artifacts,
        "wall_seconds": _positive_metric(wall_seconds, "wall_seconds"),
        "cpu_seconds": _positive_metric(cpu_seconds, "cpu_seconds"),
        "peak_rss_bytes": _positive_metric(peak_rss_bytes, "peak_rss_bytes", integer=True),
    }
    return {**body, "record_id": _hash_identity("solver-run-record", body)}


def validate_solver_run_record(value: Any) -> dict[str, Any]:
    source = _object(value, "SolverRunRecord")
    package = _object(source.get("package"), "package")
    expected = make_solver_run_record(
        plan_id=source.get("plan_id"),
        sequence=source.get("sequence"),
        run_id=source.get("run_id"),
        parent_run_id=source.get("parent_run_id"),
        action=source.get("action"),
        cause=source.get("cause"),
        package_artifact_id=package.get("artifact_id"),
        package_revision=package.get("revision"),
        numerical_profile_revision=source.get("numerical_profile_revision"),
        status=source.get("status"),
        exit_code=source.get("exit_code"),
        artifact_ids=source.get("artifact_ids"),
        wall_seconds=source.get("wall_seconds"),
        cpu_seconds=source.get("cpu_seconds"),
        peak_rss_bytes=source.get("peak_rss_bytes"),
    )
    if source.get("contract_version") != SESSION_CONTRACT_VERSION:
        raise ContractError(
            f"SolverRunRecord contract_version must be {SESSION_CONTRACT_VERSION}"
        )
    if source.get("record_id") != expected["record_id"]:
        raise ContractError("SolverRunRecord record_id does not match its content")
    if set(source) != set(expected):
        raise ContractError("SolverRunRecord contains missing or unknown fields")
    return expected


def make_simulation_session_result(
    *,
    plan_id: str,
    attempt_id: str,
    session_ref: str,
    status: str,
    solver_run_record_ids: Sequence[str],
    journal_artifact_id: str,
    evidence_artifact_ids: Sequence[str] = (),
    terminal_cause: str | None = None,
    solver_run_records: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create the proxy's terminal or indeterminate session receipt."""

    normalized_plan = normalize_plan_id(plan_id)
    outcome = str(status).strip().lower()
    if outcome not in SESSION_RESULT_STATES:
        raise ContractError(
            "session result status must be completed, exhausted, or indeterminate"
        )
    run_records = _tokens(
        solver_run_record_ids,
        "solver_run_record_ids",
        allow_empty=outcome == "indeterminate",
    )
    if any(not _RUN_RECORD_ID.fullmatch(item) for item in run_records):
        raise ContractError("solver_run_record_ids must contain SolverRunRecord identities")
    evidence = _tokens(
        evidence_artifact_ids,
        "evidence_artifact_ids",
        allow_empty=outcome != "completed",
    )
    cause = None if terminal_cause is None else _text(terminal_cause, "terminal_cause")
    if outcome == "completed" and cause is not None:
        raise ContractError("completed session cannot have a terminal cause")
    normalized_records: list[dict[str, Any]] | None = None
    if solver_run_records is not None:
        if (
            isinstance(solver_run_records, (str, bytes, bytearray))
            or not isinstance(solver_run_records, Sequence)
        ):
            raise ContractError("solver_run_records must be an array")
        normalized_records = [
            validate_solver_run_record(record)
            for record in solver_run_records
        ]
        if not {record["record_id"] for record in normalized_records}.issubset(
            set(run_records)
        ):
            raise ContractError("solver_run_records contains unknown record_id")
    
    body = {
        "contract_version": SESSION_CONTRACT_VERSION,
        "plan_id": normalized_plan,
        "attempt_id": _identity(attempt_id, "attempt", "attempt_id"),
        "session_ref": normalize_session_ref(session_ref),
        "status": outcome,
        "solver_run_record_ids": run_records,
        "journal_artifact_id": _text(journal_artifact_id, "journal_artifact_id"),
        "evidence_artifact_ids": evidence,
        "terminal_cause": cause,
    }
    if normalized_records is not None:
        body["solver_run_records"] = normalized_records
    return {**body, "result_id": _hash_identity("simulation-result", body)}

def validate_simulation_session_result(value: Any) -> dict[str, Any]:
    source = _object(value, "SimulationSessionResult")
    expected = make_simulation_session_result(
        plan_id=source.get("plan_id"),
        attempt_id=source.get("attempt_id"),
        session_ref=source.get("session_ref"),
        status=source.get("status"),
        solver_run_record_ids=source.get("solver_run_record_ids"),
        journal_artifact_id=source.get("journal_artifact_id"),
        evidence_artifact_ids=source.get("evidence_artifact_ids"),
        terminal_cause=source.get("terminal_cause"),
        solver_run_records=source.get("solver_run_records")
        if "solver_run_records" in source
        else None,
    )
    if source.get("contract_version") != SESSION_CONTRACT_VERSION:
        raise ContractError(
            f"SimulationSessionResult contract_version must be {SESSION_CONTRACT_VERSION}"
        )
    if source.get("result_id") != expected["result_id"]:
        raise ContractError("SimulationSessionResult result_id does not match its content")
    if set(source) != set(expected):
        raise ContractError("SimulationSessionResult contains missing or unknown fields")
    return expected


