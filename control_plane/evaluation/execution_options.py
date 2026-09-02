"""Immutable execution choices and evidence-backed scheduling profiles."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from control_plane.core.evaluation_contracts import canonical_json


_REVISION = re.compile(r"^sha256:[0-9a-f]{64}$")
_OPTION_ID = re.compile(r"^execution-option:sha256:[0-9a-f]{64}$")
_PERFORMANCE_CLASS_ID = re.compile(
    r"^performance-class:sha256:[0-9a-f]{64}$"
)
_EVALUATION_ID = re.compile(r"^evaluation:[A-Za-z0-9._:-]+$")
_CANDIDATE_ID = re.compile(r"^candidate:sha256:[0-9a-f]{64}$")
_PARALLEL_EFFICIENCY_CALIBRATION_KIND = "parallel-efficiency-v1"


class ExecutionOptionError(ValueError):
    """Raised when an execution preparation contract is invalid."""


def _text(value: Any, label: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ExecutionOptionError(f"{label} is required")
    return normalized


def _revision(value: Any, label: str) -> str:
    normalized = str(value).strip().lower()
    if not _REVISION.fullmatch(normalized):
        raise ExecutionOptionError(f"{label} must be a SHA-256 revision")
    return normalized


def _performance_class_id(value: Any, label: str) -> str:
    normalized = str(value).strip().lower()
    if not _PERFORMANCE_CLASS_ID.fullmatch(normalized):
        raise ExecutionOptionError(
            f"{label} must be a performance-class SHA-256 identity"
        )
    return normalized


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ExecutionOptionError(f"{label} must be a positive integer")
    return value


def _nonnegative_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ExecutionOptionError(f"{label} must be a nonnegative integer")
    return value


def _content_id(prefix: str, value: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonical_json(dict(value)).encode("utf-8")).hexdigest()
    return f"{prefix}:sha256:{digest}"


def _copy(value: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(canonical_json(dict(value)))


def make_execution_option(
    *,
    simulation_definition_artifact_id: str,
    simulation_definition_revision: str,
    runnable_package_artifact_id: str,
    runnable_package_revision: str,
    target_id: str,
    processors: int,
    memory_bytes: int,
    performance_class_id: str,
) -> dict[str, Any]:
    """Create one content-addressed, already-approved executable choice."""

    body = {
        "schema_version": 3,
        "option_kind": "execution-option",
        "performance_class_id": _performance_class_id(
            performance_class_id,
            "execution option performance_class_id",
        ),
        "simulation_definition": {
            "artifact_id": _text(
                simulation_definition_artifact_id,
                "execution option simulation definition artifact_id",
            ),
            "revision": _revision(
                simulation_definition_revision,
                "execution option simulation definition revision",
            ),
        },
        "runnable_package": {
            "artifact_id": _text(
                runnable_package_artifact_id,
                "execution option runnable package artifact_id",
            ),
            "revision": _revision(
                runnable_package_revision,
                "execution option runnable package revision",
            ),
        },
        "target_id": _text(target_id, "execution option target_id"),
        "processors": _positive_integer(
            processors, "execution option processors"
        ),
        "memory_bytes": _positive_integer(
            memory_bytes, "execution option memory_bytes"
        ),
    }
    return {**body, "option_id": _content_id("execution-option", body)}


def validate_execution_option(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and canonically copy one ExecutionOption."""

    if not isinstance(value, Mapping):
        raise ExecutionOptionError("ExecutionOption must be an object")
    source = _copy(value)
    definition = source.get("simulation_definition")
    package = source.get("runnable_package")
    if not isinstance(definition, Mapping) or not isinstance(package, Mapping):
        raise ExecutionOptionError(
            "execution option definition and runnable package must be objects"
        )
    expected = make_execution_option(
        simulation_definition_artifact_id=definition.get("artifact_id"),
        simulation_definition_revision=definition.get("revision"),
        runnable_package_artifact_id=package.get("artifact_id"),
        runnable_package_revision=package.get("revision"),
        target_id=source.get("target_id"),
        processors=source.get("processors"),
        memory_bytes=source.get("memory_bytes"),
        performance_class_id=source.get("performance_class_id"),
    )
    if source != expected:
        raise ExecutionOptionError("ExecutionOption is invalid")
    return expected


def make_execution_option_set(
    options: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Create an unordered set of execution-equivalent approved choices."""

    if isinstance(options, (str, bytes, bytearray)) or not isinstance(
        options, Sequence
    ):
        raise ExecutionOptionError("execution options must be an array")
    normalized = [validate_execution_option(item) for item in options]
    if not normalized:
        raise ExecutionOptionError("ExecutionOptionSet must not be empty")
    option_ids = [item["option_id"] for item in normalized]
    if len(option_ids) != len(set(option_ids)):
        raise ExecutionOptionError("ExecutionOptionSet contains duplicate options")
    definitions = {
        canonical_json(item["simulation_definition"]) for item in normalized
    }
    if len(definitions) != 1:
        raise ExecutionOptionError(
            "ExecutionOptionSet options must share one simulation definition"
        )
    body = {
        "schema_version": 2,
        "option_set_kind": "execution-option-set",
        "options": sorted(normalized, key=lambda item: item["option_id"]),
    }
    return {**body, "option_set_id": _content_id("execution-option-set", body)}


def validate_execution_option_set(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and canonically copy one ExecutionOptionSet."""

    if not isinstance(value, Mapping):
        raise ExecutionOptionError("ExecutionOptionSet must be an object")
    source = _copy(value)
    options = source.get("options")
    if not isinstance(options, list):
        raise ExecutionOptionError("execution option set options must be an array")
    expected = make_execution_option_set(options)
    if source != expected:
        raise ExecutionOptionError("ExecutionOptionSet is invalid")
    return expected


def make_performance_profile(
    *,
    execution_option_id: str,
    evidence_artifact_id: str,
    evidence_revision: str,
    sample_count: int,
    duration_p50_seconds: int,
    duration_p90_seconds: int,
    peak_rss_p90_bytes: int,
    performance_class_id: str,
    success_rate_ppm: int = 1_000_000,
) -> dict[str, Any]:
    """Describe observed scheduling performance without changing option identity."""

    option_id = _text(execution_option_id, "performance profile option_id")
    if not _OPTION_ID.fullmatch(option_id):
        raise ExecutionOptionError("performance profile option_id is invalid")
    p50 = _positive_integer(
        duration_p50_seconds, "performance profile duration_p50_seconds"
    )
    p90 = _positive_integer(
        duration_p90_seconds, "performance profile duration_p90_seconds"
    )
    if p90 < p50:
        raise ExecutionOptionError("performance profile p90 cannot be below p50")
    rate = _positive_integer(success_rate_ppm, "performance profile success_rate_ppm")
    if rate > 1_000_000:
        raise ExecutionOptionError("performance profile success_rate_ppm exceeds one")
    body = {
        "schema_version": 3,
        "profile_kind": "execution-performance-profile",
        "execution_option_id": option_id,
        "performance_class_id": _performance_class_id(
            performance_class_id,
            "performance profile performance_class_id",
        ),
        "evidence": {
            "artifact_id": _text(
                evidence_artifact_id,
                "performance profile evidence artifact_id",
            ),
            "revision": _revision(
                evidence_revision, "performance profile evidence revision"
            ),
        },
        "sample_count": _nonnegative_integer(
            sample_count, "performance profile sample_count"
        ),
        "duration_p50_seconds": p50,
        "duration_p90_seconds": p90,
        "peak_rss_p90_bytes": _positive_integer(
            peak_rss_p90_bytes, "performance profile peak_rss_p90_bytes"
        ),
        "success_rate_ppm": rate,
    }
    return {**body, "profile_id": _content_id("performance-profile", body)}


def validate_performance_profile(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ExecutionOptionError("PerformanceProfile must be an object")
    source = _copy(value)
    evidence = source.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ExecutionOptionError("PerformanceProfile evidence must be an object")
    expected = make_performance_profile(
        execution_option_id=source.get("execution_option_id"),
        evidence_artifact_id=evidence.get("artifact_id"),
        evidence_revision=evidence.get("revision"),
        sample_count=source.get("sample_count"),
        duration_p50_seconds=source.get("duration_p50_seconds"),
        duration_p90_seconds=source.get("duration_p90_seconds"),
        peak_rss_p90_bytes=source.get("peak_rss_p90_bytes"),
        success_rate_ppm=source.get("success_rate_ppm"),
        performance_class_id=source.get("performance_class_id"),
    )
    if source != expected:
        raise ExecutionOptionError("PerformanceProfile is invalid")
    return expected


def make_performance_profile_snapshot(
    *,
    policy_revision: str,
    profiles: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Freeze the evidence used by one deterministic scheduling decision."""

    if isinstance(profiles, (str, bytes, bytearray)) or not isinstance(
        profiles, Sequence
    ):
        raise ExecutionOptionError("performance profiles must be an array")
    normalized = [validate_performance_profile(item) for item in profiles]
    if not normalized:
        raise ExecutionOptionError("PerformanceProfileSnapshot must not be empty")
    option_ids = [item["execution_option_id"] for item in normalized]
    if len(option_ids) != len(set(option_ids)):
        raise ExecutionOptionError(
            "PerformanceProfileSnapshot contains duplicate option profiles"
        )
    body = {
        "schema_version": 1,
        "snapshot_kind": "execution-performance-profile-snapshot",
        "policy_revision": _revision(
            policy_revision, "performance profile policy_revision"
        ),
        "profiles": sorted(
            normalized, key=lambda item: item["execution_option_id"]
        ),
    }
    return {
        **body,
        "profile_snapshot_id": _content_id("performance-profile-snapshot", body),
    }


def validate_performance_profile_snapshot(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ExecutionOptionError("PerformanceProfileSnapshot must be an object")
    source = _copy(value)
    profiles = source.get("profiles")
    if not isinstance(profiles, list):
        raise ExecutionOptionError("performance profile snapshot profiles must be an array")
    expected = make_performance_profile_snapshot(
        policy_revision=source.get("policy_revision"), profiles=profiles
    )
    if source != expected:
        raise ExecutionOptionError("PerformanceProfileSnapshot is invalid")
    return expected


def make_parallel_efficiency_calibration(
    *,
    replicate_ordinal: int,
    selected_processors: int,
    unmeasured_processors: Sequence[int],
    target_isolation: str = "exclusive",
) -> dict[str, Any]:
    """Attest one bounded P1/P2/P4 measurement selected by project policy."""

    if isinstance(unmeasured_processors, (str, bytes, bytearray)) or not isinstance(
        unmeasured_processors, Sequence
    ):
        raise ExecutionOptionError(
            "parallel-efficiency calibration unmeasured_processors must be an array"
        )
    unmeasured = sorted(
        _positive_integer(
            value, "parallel-efficiency calibration unmeasured_processors item"
        )
        for value in unmeasured_processors
    )
    if (
        not unmeasured
        or any(value == 1 for value in unmeasured)
        or len(unmeasured) != len(set(unmeasured))
    ):
        raise ExecutionOptionError(
            "parallel-efficiency calibration must name unique multi-processor shapes"
        )
    if target_isolation != "exclusive":
        raise ExecutionOptionError(
            "parallel-efficiency calibration requires exclusive target isolation"
        )
    return {
        "schema_version": 1,
        "calibration_kind": _PARALLEL_EFFICIENCY_CALIBRATION_KIND,
        "replicate_ordinal": _positive_integer(
            replicate_ordinal, "parallel-efficiency calibration replicate_ordinal"
        ),
        "selected_processors": _positive_integer(
            selected_processors, "parallel-efficiency calibration selected_processors"
        ),
        "unmeasured_processors": unmeasured,
        "target_isolation": "exclusive",
    }


def validate_parallel_efficiency_calibration(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the exact calibration exception embedded in a Preparation."""

    if not isinstance(value, Mapping):
        raise ExecutionOptionError("parallel-efficiency calibration must be an object")
    source = _copy(value)
    expected = make_parallel_efficiency_calibration(
        replicate_ordinal=source.get("replicate_ordinal"),
        selected_processors=source.get("selected_processors"),
        unmeasured_processors=source.get("unmeasured_processors"),
        target_isolation=source.get("target_isolation"),
    )
    if source != expected:
        raise ExecutionOptionError("parallel-efficiency calibration is invalid")
    return expected


def make_execution_preparation(
    *,
    evaluation_id: str,
    candidate_id: str,
    simulation_proxy: str,
    numerical_profile: str = "default",
    recovery_profile_revision: str = "sha256:" + "0" * 64,
    command_timeout_seconds: int,
    max_solver_runs: int,
    max_wall_seconds: int,
    execution_option_set: Mapping[str, Any],
    performance_profile_snapshot: Mapping[str, Any],
    calibration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind an evaluation to adapter-derived execution choices.

    ``numerical_profile`` defaults to ``"default"`` and
    ``recovery_profile_revision`` to the all-zero SHA-256 revision when an
    adapter does not publish these optional fields.
    """
    """Bind scientific identity to approved choices without selecting one."""

    evaluation = _text(evaluation_id, "execution preparation evaluation_id")
    candidate = _text(candidate_id, "execution preparation candidate_id").lower()
    if not _EVALUATION_ID.fullmatch(evaluation):
        raise ExecutionOptionError("execution preparation evaluation_id is invalid")
    if not _CANDIDATE_ID.fullmatch(candidate):
        raise ExecutionOptionError("execution preparation candidate_id is invalid")
    option_set = validate_execution_option_set(execution_option_set)
    profiles = validate_performance_profile_snapshot(performance_profile_snapshot)
    options_by_id = {item["option_id"]: item for item in option_set["options"]}
    profiles_by_id = {
        item["execution_option_id"]: item for item in profiles["profiles"]
    }
    if set(options_by_id) != set(profiles_by_id):
        raise ExecutionOptionError(
            "performance profile snapshot must cover every execution option exactly"
        )
    if any(
        profiles_by_id[option_id]["performance_class_id"]
        != options_by_id[option_id]["performance_class_id"]
        for option_id in options_by_id
    ):
        raise ExecutionOptionError(
            "execution option and performance profile classes must match"
        )
    if any(
        profiles_by_id[option_id]["peak_rss_p90_bytes"]
        > options_by_id[option_id]["memory_bytes"]
        for option_id in options_by_id
    ):
        raise ExecutionOptionError(
            "execution option memory must cover its profiled p90 peak RSS"
        )
    calibration_value = (
        None
        if calibration is None
        else validate_parallel_efficiency_calibration(calibration)
    )
    solver_runs = _positive_integer(
        max_solver_runs, "execution preparation max_solver_runs"
    )
    if calibration_value is not None and solver_runs != 1:
        raise ExecutionOptionError(
            "parallel-efficiency calibration requires exactly one solver run"
        )
    unmeasured_processors = {
        option["processors"]
        for option_id, option in options_by_id.items()
        if option["processors"] > 1
        and profiles_by_id[option_id]["sample_count"] < 1
    }
    if unmeasured_processors and (
        calibration_value is None
        or unmeasured_processors
        != set(calibration_value["unmeasured_processors"])
    ):
        raise ExecutionOptionError(
            "multi-processor options require measured performance evidence"
        )
    if calibration_value is not None and unmeasured_processors != set(
        calibration_value["unmeasured_processors"]
    ):
        raise ExecutionOptionError(
            "parallel-efficiency calibration unmeasured shapes do not match options"
        )
    if calibration_value is not None:
        selected = calibration_value["selected_processors"]
        selected_options = [
            option for option in options_by_id.values() if option["processors"] == selected
        ]
        if len(selected_options) != 1:
            raise ExecutionOptionError(
                "parallel-efficiency calibration must select one approved option"
            )

    body = {
        "schema_version": 1,
        "preparation_kind": "simulation-execution-preparation",
        "evaluation_id": evaluation,
        "candidate_id": candidate,
        "simulation_proxy": _text(
            simulation_proxy, "execution preparation simulation_proxy"
        ),
        "numerical_profile": _text(
            numerical_profile, "execution preparation numerical_profile"
        ),
        "recovery_profile_revision": _revision(
            recovery_profile_revision,
            "execution preparation recovery_profile_revision",
        ),
        "budget": {
            "command_timeout_seconds": _positive_integer(
                command_timeout_seconds,
                "execution preparation command_timeout_seconds",
            ),
            "max_solver_runs": _positive_integer(
                solver_runs, "execution preparation max_solver_runs"
            ),
            "max_wall_seconds": _positive_integer(
                max_wall_seconds, "execution preparation max_wall_seconds"
            ),
        },
        "execution_option_set": option_set,
        "performance_profile_snapshot": profiles,
    }
    if calibration_value is not None:
        body["calibration"] = calibration_value
    return {**body, "preparation_id": _content_id("execution-preparation", body)}


def validate_execution_preparation(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ExecutionOptionError("ExecutionPreparation must be an object")
    source = _copy(value)
    budget = source.get("budget")
    if not isinstance(budget, Mapping):
        raise ExecutionOptionError("ExecutionPreparation budget is invalid")
    expected = make_execution_preparation(
        evaluation_id=source.get("evaluation_id"),
        candidate_id=source.get("candidate_id"),
        simulation_proxy=source.get("simulation_proxy"),
        numerical_profile=source.get("numerical_profile"),
        recovery_profile_revision=source.get("recovery_profile_revision"),
        command_timeout_seconds=budget.get("command_timeout_seconds"),
        max_solver_runs=budget.get("max_solver_runs"),
        max_wall_seconds=budget.get("max_wall_seconds"),
        execution_option_set=source.get("execution_option_set"),
        performance_profile_snapshot=source.get("performance_profile_snapshot"),
        calibration=source.get("calibration"),
    )
    if source != expected:
        raise ExecutionOptionError("ExecutionPreparation is invalid")
    return expected
