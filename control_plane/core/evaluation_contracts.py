"""Versioned, simulator-neutral contracts for scientific evaluations."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Any


CONTRACT_VERSION = 1
EVALUATION_STATES = frozenset(
    {
        "requested",
        "deduplicating",
        "queued",
        "running",
        "recovering",
        "qualifying",
        "qualified",
        "ambiguous",
        "unresolved",
        "cancelled",
    }
)
ATTEMPT_STATES = frozenset(
    {
        "planned",
        "starting",
        "unconfirmed",
        "leased",
        "running",
        "reconciling",
        "collecting",
        "completed",
        "failed",
        "lost",
        "cancelled",
    }
)
# Attempt termination states: requested means control-plane termination was issued;
# confirmed means the remote side acknowledged termination;
# unavailable means the adapter cannot terminate, so confirmation is impossible and
# this state is used as evidence when judging whether the session leaked.
# Cancel failures or uncertain outcomes stay requested and are retried next round;
# no fourth state is needed.
ATTEMPT_TERMINATION_STATES = frozenset({"requested", "confirmed", "unavailable"})
_ATTEMPT_TERMINATION_STATE_SQL_ORDER = ("requested", "confirmed", "unavailable")


def attempt_termination_states_sql(states: frozenset[str]) -> str:
    """Render attempt termination states as deterministic SQL string literals."""
    return ", ".join(
        f"'{state}'"
        for state in _ATTEMPT_TERMINATION_STATE_SQL_ORDER
        if state in states
    )

# Attempt states whose terminal transition records a requested termination.
TERMINATION_REQUEST_SOURCE_STATES = frozenset(
    {"starting", "running", "collecting", "reconciling"}
)

# States in which an Attempt is active and blocks preparation reuse; starting and
# unconfirmed are included because dispatch work is in-flight before lease confirmation.
ACTIVE_ATTEMPT_STATES = frozenset(
    {
        "planned",
        "starting",
        "unconfirmed",
        "leased",
        "running",
        "reconciling",
        "collecting",
    }
)
# States that retain a capacity allocation; planned has none, while starting and
# unconfirmed reserve capacity even before a worker lease is confirmed.
CAPACITY_HOLDING_ATTEMPT_STATES = frozenset(
    {"starting", "unconfirmed", "leased", "running", "collecting", "reconciling"}
)
# States whose owner is expected to renew a lease; starting is worker-owned, while
# unconfirmed is capacity-held but has no confirmed owner and therefore no heartbeat.
HEARTBEATABLE_ATTEMPT_STATES = frozenset({"starting", "leased", "running", "collecting"})


_ATTEMPT_STATE_SQL_ORDER = (
    "planned",
    "starting",
    "unconfirmed",
    "leased",
    "running",
    "reconciling",
    "collecting",
    "completed",
    "failed",
    "lost",
    "cancelled",
)


def attempt_states_sql(states: frozenset[str]) -> str:
    """Render attempt states as deterministic SQL string literals."""
    return ", ".join(
        f"'{state}'"
        for state in _ATTEMPT_STATE_SQL_ORDER
        if state in states
    )
QUALIFICATION_STATES = frozenset({"qualified", "ambiguous", "rejected"})
ALGORITHM_RUN_STATES = frozenset({"active", "completed", "blocked", "failed"})
ALGORITHM_RETENTION_CLASSES = frozenset(
    {"project-lifetime", "permanent", "regenerable"}
)

_REVISION = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_OBSERVATION_ID = re.compile(r"^observation:sha256:[0-9a-f]{64}$")


class ContractError(ValueError):
    """Raised when an evaluation contract is malformed or self-inconsistent."""


def canonical_json(value: Any) -> str:
    """Return the sole JSON representation used for identities and persistence."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContractError("value must be finite canonical JSON") from exc


def _json_copy(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{label} must be an object")
    return dict(value)


def _text(value: Any, label: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text or not _TOKEN.fullmatch(text):
        raise ContractError(f"{label} must be a non-empty stable token")
    return text


def normalize_token(value: Any, label: str = "value") -> str:
    """Validate a stable control-plane token without assigning domain meaning."""

    return _text(value, label)


def _revision(value: Any, label: str) -> str:
    revision = str(value).strip().lower()
    if not _REVISION.fullmatch(revision):
        raise ContractError(f"{label} must be sha256:<64 lowercase hex characters>")
    return revision


def _require_contract_version(value: Any, label: str) -> None:
    if type(value) is not int or value != CONTRACT_VERSION:
        raise ContractError(f"{label} contract_version must be {CONTRACT_VERSION}")


def _uuid_identity(value: Any, prefix: str, label: str) -> str:
    text = str(value).strip().lower()
    expected_prefix = f"{prefix}:"
    if not text.startswith(expected_prefix):
        raise ContractError(f"{label} must start with {expected_prefix}")
    try:
        parsed = uuid.UUID(text[len(expected_prefix) :])
    except ValueError as exc:
        raise ContractError(f"{label} must contain a UUID") from exc
    canonical = expected_prefix + str(parsed)
    if text != canonical:
        raise ContractError(f"{label} must use canonical lowercase UUID form")
    return canonical


def _hash_identity(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}:sha256:{digest}"


def _hash_key(value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _unique_tokens(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ContractError(f"{label} must be an array")
    tokens = [_text(item, f"{label}[{index}]") for index, item in enumerate(value)]
    if not allow_empty and not tokens:
        raise ContractError(f"{label} must not be empty")
    if len(tokens) != len(set(tokens)):
        raise ContractError(f"{label} must not contain duplicates")
    return sorted(tokens)


def _artifact_refs(value: Any, label: str) -> list[dict[str, str]]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ContractError(f"{label} must be an array")
    refs: list[dict[str, str]] = []
    for index, item in enumerate(value):
        source = _object(item, f"{label}[{index}]")
        if set(source) != {"artifact_id", "revision"}:
            raise ContractError(
                f"{label}[{index}] must contain artifact_id and revision"
            )
        refs.append(
            {
                "artifact_id": _text(
                    source.get("artifact_id"), f"{label}[{index}].artifact_id"
                ),
                "revision": _revision(
                    source.get("revision"), f"{label}[{index}].revision"
                ),
            }
        )
    canonical = sorted(refs, key=lambda item: (item["artifact_id"], item["revision"]))
    if len({(item["artifact_id"], item["revision"]) for item in canonical}) != len(
        canonical
    ):
        raise ContractError(f"{label} must not contain duplicates")
    return canonical


def _metrics(
    value: Any, label: str = "metrics", *, allow_empty: bool = False
) -> dict[str, float]:
    source = _object(value, label)
    if not source and not allow_empty:
        raise ContractError(f"{label} must not be empty")
    result: dict[str, float] = {}
    for name, raw in source.items():
        key = _text(name, f"{label} name")
        if isinstance(raw, bool):
            raise ContractError(f"{label}.{key} must be numeric")
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise ContractError(f"{label}.{key} must be numeric") from exc
        if not math.isfinite(number):
            raise ContractError(f"{label}.{key} must be finite")
        result[key] = number
    return dict(sorted(result.items()))


def make_problem_definition(
    *,
    problem_id: str,
    parameter_schema_revision: str,
    constraint_revision: str,
    simulation_capabilities: Sequence[str],
    metric_schema_revision: str,
) -> dict[str, Any]:
    body = {
        "contract_version": CONTRACT_VERSION,
        "problem_id": _text(problem_id, "problem_id"),
        "parameter_schema_revision": _revision(
            parameter_schema_revision, "parameter_schema_revision"
        ),
        "constraint_revision": _revision(constraint_revision, "constraint_revision"),
        "simulation_capabilities": _unique_tokens(
            simulation_capabilities, "simulation_capabilities"
        ),
        "metric_schema_revision": _revision(
            metric_schema_revision, "metric_schema_revision"
        ),
    }
    return {**body, "revision": _hash_key(body)}


def validate_problem_definition(value: Any) -> dict[str, Any]:
    source = _object(value, "ProblemDefinition")
    expected = make_problem_definition(
        problem_id=source.get("problem_id"),
        parameter_schema_revision=source.get("parameter_schema_revision"),
        constraint_revision=source.get("constraint_revision"),
        simulation_capabilities=source.get("simulation_capabilities"),
        metric_schema_revision=source.get("metric_schema_revision"),
    )
    _require_contract_version(source.get("contract_version"), "ProblemDefinition")
    if str(source.get("revision", "")).lower() != expected["revision"]:
        raise ContractError("ProblemDefinition revision does not match its canonical content")
    if set(source) != set(expected):
        raise ContractError("ProblemDefinition contains missing or unknown fields")
    return expected


def make_candidate(
    *, problem_id: str, problem_revision: str, parameters: Mapping[str, Any]
) -> dict[str, Any]:
    body = {
        "contract_version": CONTRACT_VERSION,
        "problem_id": _text(problem_id, "problem_id"),
        "problem_revision": _revision(problem_revision, "problem_revision"),
        "parameters": _json_copy(_object(parameters, "parameters")),
    }
    return {**body, "candidate_id": _hash_identity("candidate", body)}


def validate_candidate(value: Any) -> dict[str, Any]:
    source = _object(value, "Candidate")
    expected = make_candidate(
        problem_id=source.get("problem_id"),
        problem_revision=source.get("problem_revision"),
        parameters=source.get("parameters"),
    )
    _require_contract_version(source.get("contract_version"), "Candidate")
    if source.get("candidate_id") != expected["candidate_id"]:
        raise ContractError("Candidate ID does not match its canonical physical definition")
    if set(source) != set(expected):
        raise ContractError("Candidate contains missing or unknown fields")
    return expected


def make_evaluation_request(
    *,
    candidate_id: str,
    fidelity: str,
    requested_outputs: Sequence[str],
    evidence_profile: str,
    independence_requirement: str = "normal",
    replicate_key: str | None = None,
    priority: str = "normal",
    evaluation_id: str | None = None,
) -> dict[str, Any]:
    candidate = _text(candidate_id, "candidate_id")
    independence = str(independence_requirement).strip().lower()
    if independence not in {"normal", "independent"}:
        raise ContractError("independence_requirement must be normal or independent")
    outputs = _unique_tokens(requested_outputs, "requested_outputs")
    identity = {
        "candidate_id": candidate,
        "fidelity": _text(fidelity, "fidelity"),
        "requested_outputs": outputs,
        "evidence_profile": _text(evidence_profile, "evidence_profile"),
        "independence_requirement": independence,
    }
    if replicate_key is not None:
        if independence != "independent":
            raise ContractError(
                "replicate_key is only valid for an independent EvaluationRequest"
            )
        identity["replicate_key"] = normalize_token(
            replicate_key, "replicate_key"
        )
    return {
        "contract_version": CONTRACT_VERSION,
        "evaluation_id": (
            f"evaluation:{uuid.uuid4()}"
            if evaluation_id is None
            else _uuid_identity(evaluation_id, "evaluation", "evaluation_id")
        ),
        "idempotency_key": _hash_key(identity),
        **identity,
        "priority": _text(priority, "priority"),
    }


def validate_evaluation_request(value: Any) -> dict[str, Any]:
    source = _object(value, "EvaluationRequest")
    expected = make_evaluation_request(
        candidate_id=source.get("candidate_id"),
        fidelity=source.get("fidelity"),
        requested_outputs=source.get("requested_outputs"),
        evidence_profile=source.get("evidence_profile"),
        independence_requirement=source.get("independence_requirement"),
        replicate_key=source.get("replicate_key"),
        priority=source.get("priority"),
        evaluation_id=source.get("evaluation_id"),
    )
    _require_contract_version(source.get("contract_version"), "EvaluationRequest")
    if str(source.get("idempotency_key", "")).lower() != expected["idempotency_key"]:
        raise ContractError("EvaluationRequest idempotency key does not match its semantics")
    if set(source) != set(expected):
        raise ContractError("EvaluationRequest contains missing or unknown fields")
    return expected


def make_attempt(
    *,
    evaluation_id: str,
    attempt_number: int,
    simulation_adapter: str,
    numerical_profile: str,
    checkpoint_parent_attempt_id: str | None = None,
    termination_state: str | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    if isinstance(attempt_number, bool) or not isinstance(attempt_number, int) or attempt_number < 1:
        raise ContractError("attempt_number must be a positive integer")
    parent = (
        None
        if checkpoint_parent_attempt_id is None
        else _uuid_identity(
            checkpoint_parent_attempt_id,
            "attempt",
            "checkpoint_parent_attempt_id",
        )
    )
    return {
        "contract_version": CONTRACT_VERSION,
        "attempt_id": (
            f"attempt:{uuid.uuid4()}"
            if attempt_id is None
            else _uuid_identity(attempt_id, "attempt", "attempt_id")
        ),
        "evaluation_id": _uuid_identity(evaluation_id, "evaluation", "evaluation_id"),
        "attempt_number": attempt_number,
        "simulation_adapter": _text(simulation_adapter, "simulation_adapter"),
        "numerical_profile": _text(numerical_profile, "numerical_profile"),
        "checkpoint_parent_attempt_id": parent,
        "status": "planned",
        "termination_state": termination_state,
        "failure_class": None,
        "artifact_ids": [],
    }


def validate_attempt(value: Any) -> dict[str, Any]:
    source = _object(value, "Attempt")
    expected = make_attempt(
        evaluation_id=source.get("evaluation_id"),
        attempt_number=source.get("attempt_number"),
        simulation_adapter=source.get("simulation_adapter"),
        numerical_profile=source.get("numerical_profile"),
        checkpoint_parent_attempt_id=source.get("checkpoint_parent_attempt_id"),
        termination_state=source.get("termination_state"),
        attempt_id=source.get("attempt_id"),
    )
    status = str(source.get("status", "")).strip().lower()
    if status not in ATTEMPT_STATES:
        raise ContractError("Attempt status is invalid")
    termination_state = source.get("termination_state")
    if termination_state is not None:
        termination_state = str(termination_state).strip().lower()
        if termination_state not in ATTEMPT_TERMINATION_STATES:
            raise ContractError("Attempt termination_state is invalid")
    failure_class = source.get("failure_class")
    artifact_ids = _unique_tokens(source.get("artifact_ids"), "artifact_ids", allow_empty=True)
    expected.update(
        status=status,
        termination_state=termination_state,
        failure_class=failure_class,
        artifact_ids=artifact_ids,
    )
    _require_contract_version(source.get("contract_version"), "Attempt")
    if set(source) != set(expected):
        raise ContractError("Attempt contains missing or unknown fields")
    return expected


def make_qualification_report(
    *,
    evaluation_id: str,
    candidate_id: str,
    attempt_ids: Sequence[str],
    status: str,
    qualifier_revision: str,
    metric_schema_revision: str,
    metrics: Mapping[str, Any] | None = None,
    evidence_artifact_ids: Sequence[str] = (),
    issues: Sequence[str] = (),
    recoverable: bool = False,
    qualification_report_id: str | None = None,
) -> dict[str, Any]:
    qualification_status = str(status).strip().lower()
    if qualification_status not in QUALIFICATION_STATES:
        raise ContractError("qualification status must be qualified, ambiguous, or rejected")
    attempts = [
        _uuid_identity(item, "attempt", f"attempt_ids[{index}]")
        for index, item in enumerate(attempt_ids)
    ]
    if not attempts or len(attempts) != len(set(attempts)):
        raise ContractError("attempt_ids must contain unique Attempts")
    if isinstance(issues, (str, bytes, bytearray)) or not isinstance(issues, Sequence):
        raise ContractError("issues must be an array")
    normalized_issues = [str(issue).strip() for issue in issues]
    if any(not issue for issue in normalized_issues):
        raise ContractError("issues must not contain empty text")
    if not isinstance(recoverable, bool):
        raise ContractError("recoverable must be boolean")
    normalized_metrics = (
        {}
        if metrics is None
        else _metrics(metrics, allow_empty=qualification_status != "qualified")
    )
    normalized_evidence = _unique_tokens(
        evidence_artifact_ids, "evidence_artifact_ids", allow_empty=True
    )
    if qualification_status == "qualified":
        if not normalized_metrics:
            raise ContractError("qualified report requires metrics")
        if not normalized_evidence:
            raise ContractError("qualified report requires evidence artifacts")
        if normalized_issues:
            raise ContractError("qualified report cannot contain issues")
        if recoverable:
            raise ContractError("qualified report cannot be recoverable")
    elif not normalized_issues:
        raise ContractError("ambiguous or rejected report requires at least one issue")
    if qualification_status == "ambiguous" and recoverable:
        raise ContractError("ambiguous report is terminal and cannot be recoverable")
    return {
        "contract_version": CONTRACT_VERSION,
        "qualification_report_id": (
            f"qualification:{uuid.uuid4()}"
            if qualification_report_id is None
            else _uuid_identity(
                qualification_report_id,
                "qualification",
                "qualification_report_id",
            )
        ),
        "evaluation_id": _uuid_identity(evaluation_id, "evaluation", "evaluation_id"),
        "candidate_id": _text(candidate_id, "candidate_id"),
        "attempt_ids": attempts,
        "status": qualification_status,
        "recoverable": recoverable,
        "metrics": normalized_metrics,
        "evidence_artifact_ids": normalized_evidence,
        "issues": normalized_issues,
        "qualifier_revision": _revision(qualifier_revision, "qualifier_revision"),
        "metric_schema_revision": _revision(
            metric_schema_revision, "metric_schema_revision"
        ),
    }


def validate_qualification_report(value: Any) -> dict[str, Any]:
    source = _object(value, "QualificationReport")
    expected = make_qualification_report(
        evaluation_id=source.get("evaluation_id"),
        candidate_id=source.get("candidate_id"),
        attempt_ids=source.get("attempt_ids"),
        status=source.get("status"),
        qualifier_revision=source.get("qualifier_revision"),
        metric_schema_revision=source.get("metric_schema_revision"),
        metrics=source.get("metrics"),
        evidence_artifact_ids=source.get("evidence_artifact_ids"),
        issues=source.get("issues"),
        recoverable=source.get("recoverable"),
        qualification_report_id=source.get("qualification_report_id"),
    )
    _require_contract_version(source.get("contract_version"), "QualificationReport")
    if set(source) != set(expected):
        raise ContractError("QualificationReport contains missing or unknown fields")
    return expected


def observation_from_report(report: Mapping[str, Any]) -> dict[str, Any]:
    qualified = validate_qualification_report(report)
    if qualified["status"] != "qualified":
        raise ContractError("only a qualified report can create an Observation")
    body = {
        "contract_version": CONTRACT_VERSION,
        "evaluation_id": qualified["evaluation_id"],
        "candidate_id": qualified["candidate_id"],
        "qualification_status": "qualified",
        "metrics": qualified["metrics"],
        "evidence_artifact_ids": qualified["evidence_artifact_ids"],
        "attempt_ids": qualified["attempt_ids"],
        "qualifier_revision": qualified["qualifier_revision"],
        "metric_schema_revision": qualified["metric_schema_revision"],
        # Advisory provenance only: deliberately excluded from the identity body.
        "extractor_revision": qualified["qualifier_revision"],
    }
    identity_body = {
        key: value for key, value in body.items() if key != "extractor_revision"
    }
    return {**body, "observation_id": _hash_identity("observation", identity_body)}


def validate_observation(value: Any) -> dict[str, Any]:
    source = _object(value, "Observation")
    if source.get("qualification_status") != "qualified":
        raise ContractError("Observation must be qualified")
    extractor_revision = source.get("extractor_revision")
    if extractor_revision is not None and not isinstance(extractor_revision, str):
        raise ContractError("Observation extractor_revision must be a string or null")
    report = make_qualification_report(
        evaluation_id=source.get("evaluation_id"),
        candidate_id=source.get("candidate_id"),
        attempt_ids=source.get("attempt_ids"),
        status="qualified",
        qualifier_revision=source.get("qualifier_revision"),
        metric_schema_revision=source.get("metric_schema_revision"),
        metrics=source.get("metrics"),
        evidence_artifact_ids=source.get("evidence_artifact_ids"),
    )
    expected = observation_from_report(report)
    # Existing observations omit this advisory field; preserve that wire shape.
    if "extractor_revision" in source:
        expected["extractor_revision"] = extractor_revision
    else:
        expected.pop("extractor_revision")
    _require_contract_version(source.get("contract_version"), "Observation")
    identity_body = {
        key: item
        for key, item in expected.items()
        if key not in {"observation_id", "extractor_revision"}
    }
    if source.get("observation_id") != _hash_identity("observation", identity_body):
        raise ContractError("Observation ID does not match its canonical evidence content")
    if set(source) != set(expected):
        raise ContractError("Observation contains missing or unknown fields")
    return expected


def make_algorithm_run(
    *,
    algorithm_run_id: str,
    algorithm_id: str,
    algorithm_revision: str,
    problem_id: str,
    problem_revision: str,
    configuration: Mapping[str, Any],
    input_artifact_refs: Sequence[Mapping[str, Any]],
    retention_class: str,
) -> dict[str, Any]:
    normalized_configuration = _json_copy(_object(configuration, "configuration"))
    if not normalized_configuration:
        raise ContractError("configuration must not be empty")
    retention = _text(retention_class, "retention_class")
    if retention not in ALGORITHM_RETENTION_CLASSES:
        raise ContractError(
            "retention_class must be project-lifetime, permanent, or regenerable"
        )
    return {
        "contract_version": CONTRACT_VERSION,
        "algorithm_run_id": _text(algorithm_run_id, "algorithm_run_id"),
        "algorithm_id": _text(algorithm_id, "algorithm_id"),
        "algorithm_revision": _revision(
            algorithm_revision, "algorithm_revision"
        ),
        "problem_id": _text(problem_id, "problem_id"),
        "problem_revision": _revision(problem_revision, "problem_revision"),
        "configuration_revision": _hash_key(normalized_configuration),
        "configuration": normalized_configuration,
        "input_artifact_refs": _artifact_refs(
            input_artifact_refs, "input_artifact_refs"
        ),
        "retention_class": retention,
    }


def validate_algorithm_run(value: Any) -> dict[str, Any]:
    source = _object(value, "AlgorithmRun")
    expected = make_algorithm_run(
        algorithm_run_id=source.get("algorithm_run_id"),
        algorithm_id=source.get("algorithm_id"),
        algorithm_revision=source.get("algorithm_revision"),
        problem_id=source.get("problem_id"),
        problem_revision=source.get("problem_revision"),
        configuration=source.get("configuration"),
        input_artifact_refs=source.get("input_artifact_refs"),
        retention_class=source.get("retention_class"),
    )
    _require_contract_version(source.get("contract_version"), "AlgorithmRun")
    if source.get("configuration_revision") != expected["configuration_revision"]:
        raise ContractError(
            "AlgorithmRun configuration_revision does not match configuration"
        )
    if set(source) != set(expected):
        raise ContractError("AlgorithmRun contains missing or unknown fields")
    return expected


def make_algorithm_event(
    *,
    algorithm_run_id: str,
    event_key: str,
    event_type: str,
    run_status: str,
    payload_schema_revision: str,
    input_observation_ids: Sequence[str],
    artifact_ids: Sequence[str],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    status = _text(run_status, "run_status")
    if status not in ALGORITHM_RUN_STATES:
        raise ContractError(
            "run_status must be active, completed, blocked, or failed"
        )
    observations = _unique_tokens(
        input_observation_ids, "input_observation_ids", allow_empty=True
    )
    if any(not _OBSERVATION_ID.fullmatch(item) for item in observations):
        raise ContractError(
            "input_observation_ids must contain canonical Observation IDs"
        )
    normalized_payload = _json_copy(_object(payload, "payload"))
    if not normalized_payload:
        raise ContractError("payload must not be empty")
    body = {
        "contract_version": CONTRACT_VERSION,
        "algorithm_run_id": _text(algorithm_run_id, "algorithm_run_id"),
        "event_key": _text(event_key, "event_key"),
        "event_type": _text(event_type, "event_type"),
        "run_status": status,
        "payload_schema_revision": _revision(
            payload_schema_revision, "payload_schema_revision"
        ),
        "input_observation_ids": observations,
        "artifact_ids": _unique_tokens(
            artifact_ids, "artifact_ids", allow_empty=True
        ),
        "payload": normalized_payload,
    }
    return {**body, "algorithm_event_id": _hash_identity("algorithm-event", body)}


def validate_algorithm_event(value: Any) -> dict[str, Any]:
    source = _object(value, "AlgorithmEvent")
    expected = make_algorithm_event(
        algorithm_run_id=source.get("algorithm_run_id"),
        event_key=source.get("event_key"),
        event_type=source.get("event_type"),
        run_status=source.get("run_status"),
        payload_schema_revision=source.get("payload_schema_revision"),
        input_observation_ids=source.get("input_observation_ids"),
        artifact_ids=source.get("artifact_ids"),
        payload=source.get("payload"),
    )
    _require_contract_version(source.get("contract_version"), "AlgorithmEvent")
    if source.get("algorithm_event_id") != expected["algorithm_event_id"]:
        raise ContractError("AlgorithmEvent ID does not match its canonical content")
    if set(source) != set(expected):
        raise ContractError("AlgorithmEvent contains missing or unknown fields")
    return expected


def make_algorithm_result(
    *,
    algorithm_run_id: str,
    algorithm_id: str,
    algorithm_revision: str,
    problem_id: str,
    problem_revision: str,
    result_type: str,
    input_observation_ids: Sequence[str],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    observations = _unique_tokens(
        input_observation_ids, "input_observation_ids", allow_empty=True
    )
    if any(not _OBSERVATION_ID.fullmatch(item) for item in observations):
        raise ContractError(
            "input_observation_ids must contain canonical Observation IDs"
        )
    normalized_payload = _json_copy(_object(payload, "payload"))
    if not normalized_payload:
        raise ContractError("payload must not be empty")
    body = {
        "contract_version": CONTRACT_VERSION,
        "algorithm_run_id": _text(algorithm_run_id, "algorithm_run_id"),
        "algorithm_id": _text(algorithm_id, "algorithm_id"),
        "algorithm_revision": _revision(
            algorithm_revision, "algorithm_revision"
        ),
        "problem_id": _text(problem_id, "problem_id"),
        "problem_revision": _revision(problem_revision, "problem_revision"),
        "result_type": _text(result_type, "result_type"),
        "input_observation_ids": observations,
        "payload": normalized_payload,
    }
    return {**body, "algorithm_result_id": _hash_identity("algorithm-result", body)}


def validate_algorithm_result(value: Any) -> dict[str, Any]:
    source = _object(value, "AlgorithmResult")
    expected = make_algorithm_result(
        algorithm_run_id=source.get("algorithm_run_id"),
        algorithm_id=source.get("algorithm_id"),
        algorithm_revision=source.get("algorithm_revision"),
        problem_id=source.get("problem_id"),
        problem_revision=source.get("problem_revision"),
        result_type=source.get("result_type"),
        input_observation_ids=source.get("input_observation_ids"),
        payload=source.get("payload"),
    )
    _require_contract_version(source.get("contract_version"), "AlgorithmResult")
    if source.get("algorithm_result_id") != expected["algorithm_result_id"]:
        raise ContractError("AlgorithmResult ID does not match its canonical content")
    if set(source) != set(expected):
        raise ContractError("AlgorithmResult contains missing or unknown fields")
    return expected
