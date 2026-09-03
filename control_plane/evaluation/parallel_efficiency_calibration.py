"""Task-governed contracts for serial P1/P2/P4 efficiency calibration."""

from __future__ import annotations

import re
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from control_plane.core.evaluation_contracts import (
    ContractError,
    canonical_json,
    make_evaluation_request,
    normalize_token,
    validate_candidate,
    validate_evaluation_request,
)


_CALIBRATION_KIND = "parallel-efficiency-v1"
_P4_DELAY_CALIBRATION_KIND = "parallel-efficiency-p4-delay-v1"
_TARGET_ISOLATION = "exclusive"
_CANDIDATE_ID = re.compile(r"^candidate:sha256:[0-9a-f]{64}$")
_CONFIGURATION_FIELDS = frozenset(
    {
        "schema_version",
        "calibration_kind",
        "candidate_id",
        "evidence_profile",
        "replicate_key_prefix",
        "processor_sequence",
        "unmeasured_processors",
        "fidelity",
        "requested_outputs",
        "priority",
        "target_isolation",
    }
)
_REQUEST_FIELDS = frozenset(
    {
        "contract_version",
        "evaluation_id",
        "idempotency_key",
        "candidate_id",
        "fidelity",
        "requested_outputs",
        "evidence_profile",
        "independence_requirement",
        "replicate_key",
        "priority",
    }
)
_REQUEST_NAMESPACE = uuid.UUID("a22a4e0a-265e-5df0-8436-ae9fab0d5b26")
_EXPECTED_PROCESSOR_REPLICATES = {1: 3, 2: 3, 4: 3}
_P4_DELAY_PROCESSOR_SEQUENCE = [4]


class ParallelEfficiencyCalibrationError(ValueError):
    """Raised when an efficiency-calibration request is outside its task contract."""


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ParallelEfficiencyCalibrationError(f"{label} must be a positive integer")
    return value


def _candidate_id(value: Any, label: str) -> str:
    candidate_id = str(value).strip().lower()
    if not _CANDIDATE_ID.fullmatch(candidate_id):
        raise ParallelEfficiencyCalibrationError(f"{label} must be a Candidate ID")
    return candidate_id


def _token(value: Any, label: str) -> str:
    try:
        return normalize_token(value, label)
    except ContractError as exc:
        raise ParallelEfficiencyCalibrationError(f"{label} is invalid") from exc


def validate_parallel_efficiency_calibration_configuration(
    value: Mapping[str, Any],
    *,
    candidate_id: str | None = None,
) -> dict[str, Any]:
    """Validate the complete task-attested calibration configuration."""

    if not isinstance(value, Mapping) or set(value) != _CONFIGURATION_FIELDS:
        raise ParallelEfficiencyCalibrationError(
            "parallel-efficiency calibration configuration has missing or unknown fields"
        )
    candidate = _candidate_id(value.get("candidate_id"), "candidate_id")
    if candidate_id is not None and candidate != _candidate_id(
        candidate_id, "expected candidate_id"
    ):
        raise ParallelEfficiencyCalibrationError(
            "parallel-efficiency calibration candidate differs from its task"
        )
    sequence_value = value.get("processor_sequence")
    if (
        isinstance(sequence_value, (str, bytes, bytearray))
        or not isinstance(sequence_value, Sequence)
        or not sequence_value
    ):
        raise ParallelEfficiencyCalibrationError(
            "parallel-efficiency calibration processor_sequence must be an array"
        )
    sequence = [
        _positive_integer(item, "parallel-efficiency calibration processor_sequence item")
        for item in sequence_value
    ]
    calibration_kind = _token(value.get("calibration_kind"), "calibration_kind")
    if (
        calibration_kind == _CALIBRATION_KIND
        and Counter(sequence) != _EXPECTED_PROCESSOR_REPLICATES
    ):
        raise ParallelEfficiencyCalibrationError(
            "parallel-efficiency calibration requires exactly three P1, P2, and P4 replicates"
        )
    if (
        calibration_kind == _P4_DELAY_CALIBRATION_KIND
        and sequence != _P4_DELAY_PROCESSOR_SEQUENCE
    ):
        raise ParallelEfficiencyCalibrationError(
            "parallel-efficiency P4-delay calibration requires exactly one P4 replicate"
        )
    if calibration_kind not in {
        _CALIBRATION_KIND,
        _P4_DELAY_CALIBRATION_KIND,
    }:
        raise ParallelEfficiencyCalibrationError(
            "parallel-efficiency calibration_kind is unsupported"
        )
    unmeasured_value = value.get("unmeasured_processors")
    if (
        isinstance(unmeasured_value, (str, bytes, bytearray))
        or not isinstance(unmeasured_value, Sequence)
    ):
        raise ParallelEfficiencyCalibrationError(
            "parallel-efficiency calibration unmeasured_processors must be an array"
        )
    unmeasured = sorted(
        _positive_integer(
            item,
            "parallel-efficiency calibration unmeasured_processors item",
        )
        for item in unmeasured_value
    )
    expected_unmeasured = [2, 4]
    if (
        not unmeasured
        or len(unmeasured) != len(set(unmeasured))
        or unmeasured != expected_unmeasured
    ):
        raise ParallelEfficiencyCalibrationError(
            "parallel-efficiency calibration unmeasured shapes must exactly cover its multi-processor sequence"
        )
    evidence_profile = _token(value.get("evidence_profile"), "evidence_profile")
    if (
        calibration_kind == _P4_DELAY_CALIBRATION_KIND
        and evidence_profile != _P4_DELAY_CALIBRATION_KIND
    ):
        raise ParallelEfficiencyCalibrationError(
            "parallel-efficiency P4-delay calibration requires its dedicated evidence profile"
        )
    prefix = _token(value.get("replicate_key_prefix"), "replicate_key_prefix")
    fidelity = _token(value.get("fidelity"), "fidelity")
    priority = _token(value.get("priority"), "priority")
    if fidelity != "full-tcad":
        raise ParallelEfficiencyCalibrationError(
            "parallel-efficiency calibration fidelity must be full-tcad"
        )
    if value.get("target_isolation") != _TARGET_ISOLATION:
        raise ParallelEfficiencyCalibrationError(
            "parallel-efficiency calibration requires exclusive target isolation"
        )
    try:
        probe = make_evaluation_request(
            candidate_id=candidate,
            fidelity=fidelity,
            requested_outputs=value.get("requested_outputs"),
            evidence_profile=evidence_profile,
            independence_requirement="independent",
            replicate_key=f"{prefix}-1",
            priority=priority,
            evaluation_id="evaluation:00000000-0000-5000-8000-000000000000",
        )
    except ContractError as exc:
        raise ParallelEfficiencyCalibrationError(
            "parallel-efficiency calibration EvaluationRequest fields are invalid"
        ) from exc
    normalized = {
        "schema_version": 1,
        "calibration_kind": calibration_kind,
        "candidate_id": candidate,
        "evidence_profile": evidence_profile,
        "replicate_key_prefix": prefix,
        "processor_sequence": sequence,
        "unmeasured_processors": unmeasured,
        "fidelity": fidelity,
        "requested_outputs": probe["requested_outputs"],
        "priority": priority,
        "target_isolation": _TARGET_ISOLATION,
    }
    if value != normalized:
        raise ParallelEfficiencyCalibrationError(
            "parallel-efficiency calibration configuration is not canonical"
        )
    return normalized


def build_parallel_efficiency_calibration_requests(
    candidate: Mapping[str, Any],
    *,
    task_id: str,
    configuration: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Derive all deterministic independent requests; callers do not choose a CPU shape."""

    try:
        normalized_candidate = validate_candidate(candidate)
    except ContractError as exc:
        raise ParallelEfficiencyCalibrationError("calibration Candidate is invalid") from exc
    task = _token(task_id, "task_id")
    normalized = validate_parallel_efficiency_calibration_configuration(
        configuration,
        candidate_id=normalized_candidate["candidate_id"],
    )
    requests = []
    for ordinal, processors in enumerate(
        normalized["processor_sequence"], start=1
    ):
        replicate_key = f"{normalized['replicate_key_prefix']}-{ordinal}"
        evaluation_id = "evaluation:" + str(
            uuid.uuid5(
                _REQUEST_NAMESPACE,
                canonical_json(
                    {
                        "task_id": task,
                        "configuration": normalized,
                        "candidate_id": normalized_candidate["candidate_id"],
                        "replicate_key": replicate_key,
                    }
                ),
            )
        )
        request = make_evaluation_request(
            candidate_id=normalized_candidate["candidate_id"],
            fidelity=normalized["fidelity"],
            requested_outputs=normalized["requested_outputs"],
            evidence_profile=normalized["evidence_profile"],
            independence_requirement="independent",
            replicate_key=replicate_key,
            priority=normalized["priority"],
            evaluation_id=evaluation_id,
        )
        requests.append(
            {
                "replicate_ordinal": ordinal,
                "selected_processors": processors,
                "request": request,
            }
        )
    return requests


def validate_parallel_efficiency_calibration_request(
    candidate: Mapping[str, Any],
    *,
    task_id: str,
    configuration: Mapping[str, Any],
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the one attested ordinal represented by an exact request."""

    if not isinstance(request, Mapping) or not _REQUEST_FIELDS.issubset(request):
        raise ParallelEfficiencyCalibrationError(
            "parallel-efficiency calibration EvaluationRequest is incomplete"
        )
    try:
        normalized_request = validate_evaluation_request(
            {field: request[field] for field in _REQUEST_FIELDS}
        )
    except ContractError as exc:
        raise ParallelEfficiencyCalibrationError(
            "parallel-efficiency calibration EvaluationRequest is invalid"
        ) from exc
    matches = [
        item
        for item in build_parallel_efficiency_calibration_requests(
            candidate, task_id=task_id, configuration=configuration
        )
        if item["request"] == normalized_request
    ]
    if len(matches) != 1:
        raise ParallelEfficiencyCalibrationError(
            "EvaluationRequest is not an exact task-attested calibration replicate"
        )
    return matches[0]
