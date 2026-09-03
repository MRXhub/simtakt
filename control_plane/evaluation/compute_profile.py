"""Pure, simulator-neutral compute-demand profiles for queue-aware scheduling.

This module owns the domain vocabulary for the "similar task compute profile"
closed loop:

- ``task class``: a deterministic, cheap, revision-aware identity derived from
  governed contract fields (SimulationDefinition, numerical profile, recovery
  profile revision) plus an optional strictly-bounded user class key.  It never
  includes raw Candidate parameters.
- per-processor-shape online statistics (Welford mean/variance) over real run
  feedback; wall time of successful runs and optional CPU / busy / RSS
  measurements.  ``cores * time`` is never treated as a "true compute" fact;
  it appears only as a scheduling occupancy proxy inside the Scheduler.
- a read-only capacity profile snapshot the Scheduler consumes (never I/O).
- strictly bounded task overrides (latency-vs-efficiency bias and maximum
  accepted wall uncertainty); they can never relax hard resource constraints.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from control_plane.core.evaluation_contracts import canonical_json


COMPUTE_PROFILE_CONTRACT_VERSION = 1
# R6: measured profiles only influence scheduling after five terminal
# completed samples for the task class.  Keep this policy in the contract
# layer rather than duplicating it at scheduler call sites.
MIN_TERMINAL_COMPLETION_SAMPLES = 5
DEFAULT_MIN_SAMPLES = MIN_TERMINAL_COMPLETION_SAMPLES
PRESSURE_CAP = 1.0
MAX_PROFILE_BUCKETS = 128
MAX_RECENT_FEEDBACK_PER_BUCKET = 32
MAX_SNAPSHOT_IDENTITIES = 64
MAX_WALL_SECONDS = 7 * 24 * 60 * 60
MAX_CPU_SECONDS = 7 * 24 * 60 * 60
MAX_BUSY_SECONDS = 7 * 24 * 60 * 60
MAX_RSS_BYTES = 1 << 50
MAX_AGGREGATE_COUNT = 2_000_000_000

_REVISION = re.compile(r"^sha256:[0-9a-f]{64}$")
_TASK_CLASS_KEY = re.compile(r"^task-class:sha256:[0-9a-f]{64}$")
_PROFILE_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_USER_CLASS_KEY = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")
_SOURCE_VALUES = frozenset(
    {"measured", "measured-uncertainty-capped", "evidence-bootstrap"}
)


class ComputeProfileError(ValueError):
    """Raised when a compute-profile contract is malformed or inconsistent."""


def _text(value: Any, label: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ComputeProfileError(f"{label} is required")
    return normalized


def _revision(value: Any, label: str) -> str:
    normalized = str(value).strip().lower()
    if not _REVISION.fullmatch(normalized):
        raise ComputeProfileError(f"{label} must be a SHA-256 revision")
    return normalized


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ComputeProfileError(f"{label} must be a nonnegative integer")
    return value


def _positive_int(value: Any, label: str) -> int:
    result = _nonnegative_int(value, label)
    if result == 0:
        raise ComputeProfileError(f"{label} must be a positive integer")
    return result


def _finite_nonnegative_float(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ComputeProfileError(f"{label} must be a finite nonnegative number")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ComputeProfileError(
            f"{label} must be a finite nonnegative number"
        )
    return number


def _copy(value: Any, label: str) -> Any:
    return __import__("json").loads(canonical_json(value))


def _content_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}:sha256:{digest}"


# ---------------------------------------------------------------------------
# Task class identity
# ---------------------------------------------------------------------------

def validate_user_class_key(value: Any) -> str:
    """Validate a user-supplied class key (bounded, no raw parameters)."""

    key = str(value).strip().lower()
    if not _USER_CLASS_KEY.fullmatch(key):
        raise ComputeProfileError(
            "user_class_key must be 1-64 lowercase [a-z0-9._:-] characters"
        )
    return key


def validate_task_class_key(value: Any) -> str:
    key = str(value).strip().lower()
    if not _TASK_CLASS_KEY.fullmatch(key):
        raise ComputeProfileError(
            "task_class_key must be a task-class SHA-256 identity"
        )
    return key


def make_task_class_key(
    *,
    simulation_definition_artifact_id: str,
    simulation_definition_revision: str,
    numerical_profile: str,
    recovery_profile_revision: str,
    user_class_key: str | None = None,
) -> str:
    """Return the deterministic class key for one governed task kind.

    Similar tasks (same SimulationDefinition, same numerical/recovery profile,
    optionally the same strictly-bounded user class key) share one key and one
    feedback bucket.  Raw Candidate parameters are deliberately excluded.
    """

    boundary: dict[str, Any] = {
        "simulation_definition": {
            "artifact_id": _text(
                simulation_definition_artifact_id,
                "simulation_definition_artifact_id",
            ),
            "revision": _revision(
                simulation_definition_revision,
                "simulation_definition_revision",
            ),
        },
        "numerical_profile": _text(
            numerical_profile, "numerical_profile"
        ),
        "recovery_profile_revision": _revision(
            recovery_profile_revision, "recovery_profile_revision"
        ),
    }
    if user_class_key is not None:
        boundary["user_class_key"] = validate_user_class_key(user_class_key)
    return _content_id("task-class", boundary)


def make_task_class(
    *,
    simulation_definition_artifact_id: str,
    simulation_definition_revision: str,
    numerical_profile: str,
    recovery_profile_revision: str,
    user_class_key: str | None = None,
) -> dict[str, Any]:
    """Create the validated ``task_class`` object embedded in candidates."""

    boundary: dict[str, Any] = {
        "simulation_definition": {
            "artifact_id": _text(
                simulation_definition_artifact_id,
                "simulation_definition_artifact_id",
            ),
            "revision": _revision(
                simulation_definition_revision,
                "simulation_definition_revision",
            ),
        },
        "numerical_profile": _text(numerical_profile, "numerical_profile"),
        "recovery_profile_revision": _revision(
            recovery_profile_revision, "recovery_profile_revision"
        ),
    }
    if user_class_key is not None:
        boundary["user_class_key"] = validate_user_class_key(user_class_key)
    return {
        "schema_version": COMPUTE_PROFILE_CONTRACT_VERSION,
        "key": _content_id("task-class", boundary),
        "boundary": boundary,
    }


def validate_task_class(value: Any) -> dict[str, Any]:
    """Validate and canonically copy one ``task_class`` object."""

    if not isinstance(value, Mapping):
        raise ComputeProfileError("task_class must be an object")
    source = _copy(value, "task_class")
    if source.get("schema_version") != COMPUTE_PROFILE_CONTRACT_VERSION:
        raise ComputeProfileError("task_class schema_version is invalid")
    boundary = source.get("boundary")
    if not isinstance(boundary, Mapping):
        raise ComputeProfileError("task_class boundary must be an object")
    definition = boundary.get("simulation_definition")
    if not isinstance(definition, Mapping):
        raise ComputeProfileError(
            "task_class boundary simulation_definition must be an object"
        )
    expected = make_task_class(
        simulation_definition_artifact_id=definition.get("artifact_id"),
        simulation_definition_revision=definition.get("revision"),
        numerical_profile=boundary.get("numerical_profile"),
        recovery_profile_revision=boundary.get("recovery_profile_revision"),
        user_class_key=boundary.get("user_class_key"),
    )
    if source != expected:
        raise ComputeProfileError("task_class key does not match its boundary")
    return expected


def validate_profile_revision(value: Any) -> str:
    revision = str(value).strip()
    if not _PROFILE_REVISION.fullmatch(revision):
        raise ComputeProfileError(
            "profile_revision must be a bounded stable token"
        )
    return revision


# ---------------------------------------------------------------------------
# Feedback observations
# ---------------------------------------------------------------------------

def make_feedback_observation(
    *,
    success: bool,
    wall_seconds: float | None = None,
    cpu_seconds: float | None = None,
    busy_seconds: float | None = None,
    rss_bytes: int | None = None,
) -> dict[str, Any]:
    """Validate one terminal run feedback observation (no I/O)."""

    if not isinstance(success, bool):
        raise ComputeProfileError("feedback success must be a boolean")
    wall = (
        None
        if wall_seconds is None
        else _bounded_metric(wall_seconds, "feedback wall_seconds", MAX_WALL_SECONDS)
    )
    cpu = (
        None
        if cpu_seconds is None
        else _bounded_metric(cpu_seconds, "feedback cpu_seconds", MAX_CPU_SECONDS)
    )
    busy = (
        None
        if busy_seconds is None
        else _bounded_metric(busy_seconds, "feedback busy_seconds", MAX_BUSY_SECONDS)
    )
    rss = (
        None
        if rss_bytes is None
        else _bounded_int(rss_bytes, "feedback rss_bytes", MAX_RSS_BYTES)
    )
    for metric, label in (
        (wall, "feedback wall_seconds"),
        (cpu, "feedback cpu_seconds"),
        (busy, "feedback busy_seconds"),
    ):
        if metric is not None and metric <= 0.0:
            raise ComputeProfileError(f"{label} must be positive when provided")
    if rss is not None and rss <= 0:
        raise ComputeProfileError("feedback rss_bytes must be positive when provided")
    body = {
        "success": success,
        "wall_seconds": wall,
        "cpu_seconds": cpu,
        "busy_seconds": busy,
        "rss_bytes": rss,
    }
    return _copy(body, "feedback observation")


def _bounded_metric(value: Any, label: str, maximum: float) -> float:
    number = _finite_nonnegative_float(value, label)
    if number > maximum:
        raise ComputeProfileError(f"{label} exceeds its operational bound")
    return number


def _bounded_int(value: Any, label: str, maximum: int) -> int:
    number = _nonnegative_int(value, label)
    if number > maximum:
        raise ComputeProfileError(f"{label} exceeds its operational bound")
    return number


def validate_feedback_observation(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ComputeProfileError("feedback observation must be an object")
    return make_feedback_observation(
        success=value.get("success"),
        wall_seconds=value.get("wall_seconds"),
        cpu_seconds=value.get("cpu_seconds"),
        busy_seconds=value.get("busy_seconds"),
        rss_bytes=value.get("rss_bytes"),
    )


# ---------------------------------------------------------------------------
# Online Welford statistics
# ---------------------------------------------------------------------------

def welford_update(
    mean: float | None,
    m2: float | None,
    count: int,
    value: float,
) -> tuple[float, float]:
    """Incrementally update a Welford mean/M2 accumulator."""

    if count < 0 or count >= MAX_AGGREGATE_COUNT:
        raise ComputeProfileError("Welford accumulator count exceeds bound")
    sample = _finite_nonnegative_float(value, "Welford value")
    if count == 0:
        return sample, 0.0
    if mean is None or m2 is None:
        raise ComputeProfileError("empty Welford accumulator lacks mean/M2")
    new_count = count + 1
    if not math.isfinite(mean) or not math.isfinite(m2) or m2 < 0.0:
        raise ComputeProfileError("Welford accumulator is not finite")
    delta = sample - mean
    new_mean = mean + delta / new_count
    new_m2 = m2 + delta * (float(value) - new_mean)
    if not math.isfinite(new_mean) or not math.isfinite(new_m2) or new_m2 < 0.0:
        raise ComputeProfileError("Welford update overflowed")
    return new_mean, new_m2


def welford_stddev(m2: float | None, count: int) -> float | None:
    """Population standard deviation from a Welford M2 accumulator."""

    if count <= 0 or m2 is None:
        return None
    return math.sqrt(max(0.0, m2) / count)


# ---------------------------------------------------------------------------
# Per-shape statistics accumulator and snapshot records
# ---------------------------------------------------------------------------

class TaskShapeStats:
    """Pure per-(class, target, profile revision, processors) accumulator."""

    __slots__ = (
        "task_class_key",
        "target_id",
        "profile_revision",
        "processors",
        "sample_count",
        "success_count",
        "failure_count",
        "wall_samples",
        "wall_mean",
        "wall_m2",
        "cpu_samples",
        "cpu_mean",
        "cpu_m2",
        "busy_samples",
        "busy_mean",
        "busy_m2",
        "rss_samples",
        "rss_mean",
        "rss_m2",
    )

    def __init__(
        self,
        *,
        task_class_key: str,
        target_id: str,
        profile_revision: str,
        processors: int,
    ) -> None:
        self.task_class_key = validate_task_class_key(task_class_key)
        self.target_id = _text(target_id, "target_id")
        self.profile_revision = validate_profile_revision(profile_revision)
        self.processors = _positive_int(processors, "processors")
        self.sample_count = 0
        self.success_count = 0
        self.failure_count = 0
        self.wall_samples = 0
        self.wall_mean: float | None = None
        self.wall_m2: float | None = None
        self.cpu_samples = 0
        self.cpu_mean: float | None = None
        self.cpu_m2: float | None = None
        self.busy_samples = 0
        self.busy_mean: float | None = None
        self.busy_m2: float | None = None
        self.rss_samples = 0
        self.rss_mean: float | None = None
        self.rss_m2: float | None = None

    def observe(self, observation: Mapping[str, Any]) -> "TaskShapeStats":
        feedback = validate_feedback_observation(observation)
        self.sample_count += 1
        if feedback["success"]:
            self.success_count += 1
        else:
            self.failure_count += 1
        wall = feedback["wall_seconds"]
        if feedback["success"] and wall is not None:
            self.wall_mean, self.wall_m2 = welford_update(
                self.wall_mean, self.wall_m2, self.wall_samples, wall
            )
            self.wall_samples += 1
        cpu = feedback["cpu_seconds"]
        if cpu is not None:
            self.cpu_mean, self.cpu_m2 = welford_update(
                self.cpu_mean, self.cpu_m2, self.cpu_samples, cpu
            )
            self.cpu_samples += 1
        busy = feedback["busy_seconds"]
        if busy is not None:
            self.busy_mean, self.busy_m2 = welford_update(
                self.busy_mean, self.busy_m2, self.busy_samples, busy
            )
            self.busy_samples += 1
        rss = feedback["rss_bytes"]
        if rss is not None:
            self.rss_mean, self.rss_m2 = welford_update(
                self.rss_mean, self.rss_m2, self.rss_samples, float(rss)
            )
            self.rss_samples += 1
        return self

    def shape(self) -> dict[str, Any]:
        """Return the canonical read-only per-shape snapshot record."""

        return make_shape_record(
            task_class_key=self.task_class_key,
            target_id=self.target_id,
            profile_revision=self.profile_revision,
            processors=self.processors,
            sample_count=self.sample_count,
            success_count=self.success_count,
            failure_count=self.failure_count,
            successful_wall_samples=self.wall_samples,
            successful_wall_mean_seconds=self.wall_mean,
            successful_wall_stddev_seconds=welford_stddev(
                self.wall_m2, self.wall_samples
            ),
            cpu_samples=self.cpu_samples,
            cpu_mean_seconds=self.cpu_mean,
            cpu_stddev_seconds=welford_stddev(self.cpu_m2, self.cpu_samples),
            busy_samples=self.busy_samples,
            busy_mean_seconds=self.busy_mean,
            busy_stddev_seconds=welford_stddev(self.busy_m2, self.busy_samples),
            rss_samples=self.rss_samples,
            rss_mean_bytes=self.rss_mean,
            rss_stddev_bytes=welford_stddev(self.rss_m2, self.rss_samples),
        )


def make_shape_record(
    *,
    task_class_key: str,
    target_id: str,
    profile_revision: str,
    processors: int,
    sample_count: int,
    success_count: int,
    failure_count: int,
    successful_wall_samples: int,
    successful_wall_mean_seconds: float | None,
    successful_wall_stddev_seconds: float | None,
    cpu_samples: int,
    cpu_mean_seconds: float | None,
    cpu_stddev_seconds: float | None,
    busy_samples: int,
    busy_mean_seconds: float | None,
    busy_stddev_seconds: float | None,
    rss_samples: int,
    rss_mean_bytes: float | None,
    rss_stddev_bytes: float | None,
) -> dict[str, Any]:
    samples = _nonnegative_int(sample_count, "sample_count")
    successes = _nonnegative_int(success_count, "success_count")
    failures = _nonnegative_int(failure_count, "failure_count")
    wall_samples = _nonnegative_int(
        successful_wall_samples, "successful_wall_samples"
    )
    if successes + failures != samples:
        raise ComputeProfileError(
            "success_count and failure_count must equal sample_count"
        )
    if wall_samples > successes:
        raise ComputeProfileError(
            "successful_wall_samples cannot exceed success_count"
        )
    processors_value = _positive_int(processors, "processors")
    body: dict[str, Any] = {
        "task_class_key": validate_task_class_key(task_class_key),
        "target_id": _text(target_id, "target_id"),
        "profile_revision": validate_profile_revision(profile_revision),
        "processors": processors_value,
        "sample_count": samples,
        "success_count": successes,
        "failure_count": failures,
        "successful_wall_samples": wall_samples,
        "successful_wall_mean_seconds": _optional_metric(
            successful_wall_mean_seconds, wall_samples,
            "successful_wall_mean_seconds",
        ),
        "successful_wall_stddev_seconds": _optional_metric(
            successful_wall_stddev_seconds, wall_samples,
            "successful_wall_stddev_seconds",
        ),
        "cpu_samples": _nonnegative_int(cpu_samples, "cpu_samples"),
        "cpu_mean_seconds": _optional_metric(
            cpu_mean_seconds, cpu_samples, "cpu_mean_seconds"
        ),
        "cpu_stddev_seconds": _optional_metric(
            cpu_stddev_seconds, cpu_samples, "cpu_stddev_seconds"
        ),
        "busy_samples": _nonnegative_int(busy_samples, "busy_samples"),
        "busy_mean_seconds": _optional_metric(
            busy_mean_seconds, busy_samples, "busy_mean_seconds"
        ),
        "busy_stddev_seconds": _optional_metric(
            busy_stddev_seconds, busy_samples, "busy_stddev_seconds"
        ),
        "rss_samples": _nonnegative_int(rss_samples, "rss_samples"),
        "rss_mean_bytes": _optional_metric(
            rss_mean_bytes, rss_samples, "rss_mean_bytes"
        ),
        "rss_stddev_bytes": _optional_metric(
            rss_stddev_bytes, rss_samples, "rss_stddev_bytes"
        ),
    }
    return _copy(body, "shape record")


def _optional_metric(
    value: Any, samples: int, label: str
) -> float | None:
    if samples == 0:
        if value is None:
            return None
        raise ComputeProfileError(f"{label} must be null without samples")
    if value is None:
        raise ComputeProfileError(f"{label} requires at least one sample")
    return _finite_nonnegative_float(value, label)


def validate_shape_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ComputeProfileError("shape record must be an object")
    source = _copy(value, "shape record")
    return make_shape_record(
        task_class_key=source.get("task_class_key"),
        target_id=source.get("target_id"),
        profile_revision=source.get("profile_revision"),
        processors=source.get("processors"),
        sample_count=source.get("sample_count"),
        success_count=source.get("success_count"),
        failure_count=source.get("failure_count"),
        successful_wall_samples=source.get("successful_wall_samples"),
        successful_wall_mean_seconds=source.get(
            "successful_wall_mean_seconds"
        ),
        successful_wall_stddev_seconds=source.get(
            "successful_wall_stddev_seconds"
        ),
        cpu_samples=source.get("cpu_samples"),
        cpu_mean_seconds=source.get("cpu_mean_seconds"),
        cpu_stddev_seconds=source.get("cpu_stddev_seconds"),
        busy_samples=source.get("busy_samples"),
        busy_mean_seconds=source.get("busy_mean_seconds"),
        busy_stddev_seconds=source.get("busy_stddev_seconds"),
        rss_samples=source.get("rss_samples"),
        rss_mean_bytes=source.get("rss_mean_bytes"),
        rss_stddev_bytes=source.get("rss_stddev_bytes"),
    )


def make_capacity_profile_snapshot(
    shapes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Freeze one read-only capacity profile snapshot for the Scheduler."""

    if isinstance(shapes, (str, bytes, bytearray)) or not isinstance(
        shapes, Sequence
    ):
        raise ComputeProfileError("capacity profile shapes must be an array")
    normalized = [validate_shape_record(item) for item in shapes]
    if len(normalized) > MAX_PROFILE_BUCKETS:
        raise ComputeProfileError("capacity profile snapshot exceeds bucket bound")
    keys = [
        (
            item["task_class_key"],
            item["target_id"],
            item["profile_revision"],
            item["processors"],
        )
        for item in normalized
    ]
    if len(keys) != len(set(keys)):
        raise ComputeProfileError(
            "capacity profile shapes must be unique per class/target/profile/shape"
        )
    ordered = sorted(
        normalized,
        key=lambda item: (
            item["task_class_key"],
            item["target_id"],
            item["profile_revision"],
            item["processors"],
        ),
    )
    body = {
        "schema_version": COMPUTE_PROFILE_CONTRACT_VERSION,
        "snapshot_kind": "capacity-profile-snapshot",
        "shapes": ordered,
    }
    revision = "sha256:" + hashlib.sha256(
        canonical_json(body).encode("utf-8")
    ).hexdigest()
    return {**body, "snapshot_revision": revision}


def validate_capacity_profile_snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ComputeProfileError(
            "capacity profile snapshot must be an object"
        )
    source = _copy(value, "capacity profile snapshot")
    shapes = source.get("shapes")
    if not isinstance(shapes, list):
        raise ComputeProfileError(
            "capacity profile snapshot shapes must be an array"
        )
    expected = make_capacity_profile_snapshot(shapes)
    if source.get("snapshot_revision") != expected["snapshot_revision"]:
        raise ComputeProfileError(
            "capacity profile snapshot revision does not match its content"
        )
    if set(source) != set(expected):
        raise ComputeProfileError(
            "capacity profile snapshot contains missing or unknown fields"
        )
    return expected


# ---------------------------------------------------------------------------
# Bounded task overrides
# ---------------------------------------------------------------------------

def make_task_override(
    *,
    task_class_key: str,
    latency_bias: float = 0.0,
    max_uncertainty: float | None = None,
) -> dict[str, Any]:
    """Create one strictly-bounded per-class scheduling override.

    ``latency_bias`` shifts the pressure/efficiency blend toward pure latency
    (0.0 = governed default, 1.0 = ignore efficiency).  ``max_uncertainty``
    caps the measured wall-time uncertainty margin accepted as measured.
    Overrides can never remove a hard resource/isolation filter.
    """

    bias = _finite_nonnegative_float(latency_bias, "latency_bias")
    if bias > 1.0:
        raise ComputeProfileError("latency_bias must be within 0..1")
    uncertainty: float | None = None
    if max_uncertainty is not None:
        uncertainty = _finite_nonnegative_float(
            max_uncertainty, "max_uncertainty"
        )
        if uncertainty == 0.0:
            raise ComputeProfileError(
                "max_uncertainty must be null or positive"
            )
    body = {
        "schema_version": COMPUTE_PROFILE_CONTRACT_VERSION,
        "kind": "task-override",
        "task_class_key": validate_task_class_key(task_class_key),
        "latency_bias": bias,
        "max_uncertainty": uncertainty,
    }
    return _copy(body, "task override")


def validate_task_override(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ComputeProfileError("task override must be an object")
    source = _copy(value, "task override")
    expected = make_task_override(
        task_class_key=source.get("task_class_key"),
        latency_bias=source.get("latency_bias"),
        max_uncertainty=source.get("max_uncertainty"),
    )
    if source != expected:
        raise ComputeProfileError("task override is invalid")
    return expected


# ---------------------------------------------------------------------------
# Deterministic estimation used by the Scheduler
# ---------------------------------------------------------------------------

def estimate_shape(
    profile: Mapping[str, Any],
    shape: Mapping[str, Any] | None,
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    max_uncertainty: float | None = None,
) -> dict[str, Any]:
    """Return one deterministic per-shape estimate with explicit source.

    Measured feedback wins only when the successful-wall sample count reaches
    ``min_samples``; otherwise the approved evidence profile (P90, bootstrap)
    is used and the ``fallback_reason`` records why, so bootstrap values are
    never mistaken for measured facts.
    """

    if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 1:
        raise ComputeProfileError("min_samples must be a positive integer")
    if shape is None or shape["sample_count"] == 0:
        return {
            "estimate_seconds": float(profile["duration_p90_seconds"]),
            "success_estimate": profile["success_rate_ppm"] / 1_000_000.0,
            "source": "evidence-bootstrap",
            "fallback_reason": "capacity-profile-no-samples",
        }
    success_total = int(shape["sample_count"])
    success_estimate = float(shape["success_count"]) / success_total
    if shape["successful_wall_samples"] < min_samples:
        return {
            "estimate_seconds": float(profile["duration_p90_seconds"]),
            # Wall time falls back to immutable evidence, but observed outcome
            # reliability remains the shape's own success_count/sample_count.
            "success_estimate": success_estimate,
            "source": "evidence-bootstrap",
            "fallback_reason": "insufficient-capacity-samples",
        }
    mean = shape["successful_wall_mean_seconds"]
    stddev = shape["successful_wall_stddev_seconds"]
    if max_uncertainty is not None and stddev is not None and stddev > max_uncertainty:
        return {
            "estimate_seconds": float(mean) + max_uncertainty,
            "success_estimate": success_estimate,
            "source": "measured-uncertainty-capped",
            "fallback_reason": "capacity-profile-high-uncertainty",
        }
    return {
        "estimate_seconds": float(mean),
        "success_estimate": success_estimate,
        "source": "measured",
        "fallback_reason": None,
    }
