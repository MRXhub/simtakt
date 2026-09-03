"""Deterministic learned wall-budget resolution."""
from __future__ import annotations
import math
from collections.abc import Mapping
from typing import Any


def _policy(policy: Any, name: str, default: Any) -> Any:
    if hasattr(policy, name):
        return getattr(policy, name)
    if isinstance(policy, Mapping):
        return policy.get(name, default)
    return default


def _samples(repository: Any, revision: str, fidelity: str | None, target: str | None) -> list[float]:
    rows = repository.list_completed_wall_samples(revision, fidelity, target, limit=200)
    result = []
    for row in rows:
        value = row.get("measured_wall_seconds", row.get("wall_seconds")) if isinstance(row, Mapping) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and float(value) > 0:
            result.append(float(value))
    return result


def _kills(repository: Any, revision: str, fidelity: str | None, target: str | None) -> int:
    method = getattr(repository, "count_wall_budget_kills", None) or getattr(repository, "count_wall_exceeded_attempts")
    return int(method(revision, fidelity, target))


def resolve_wall_budget(
    repository: Any,
    *,
    problem_revision: str,
    fidelity: str,
    target_id: str,
    declared_max_wall_seconds: int,
    policy: Any,
) -> dict[str, Any]:
    """Resolve one immutable budget, degrading learned specificity when sparse."""
    declared = max(1, int(declared_max_wall_seconds))
    minimum = int(_policy(policy, "min_budget_samples", 5))
    selected: list[float] = []
    source = "declared"
    for f, t, label in ((fidelity, target_id, "learned:problem+fidelity+target"), (fidelity, None, "learned:problem+fidelity"), (None, None, "learned:problem")):
        candidate = _samples(repository, problem_revision, f, t)
        if len(candidate) >= minimum:
            selected, source = candidate, label
            break
    budget = float(declared)
    widened = False
    if selected:
        ordered = sorted(selected)
        rank = max(1, math.ceil(0.95 * len(ordered))) - 1
        budget = max(budget, ordered[rank], 1.2 * max(ordered), 1.0)
        kills = _kills(repository, problem_revision, fidelity if source != "learned:problem" else None, target_id if source == "learned:problem+fidelity+target" else None)
        threshold = float(_policy(policy, "kill_rate_widen_threshold", 0.10))
        if kills / (kills + len(selected)) > threshold:
            budget *= float(_policy(policy, "kill_widen_factor", 1.5))
            widened = True
    kill_multiplier = float(_policy(policy, "kill_multiplier", 1.7))
    stall_fraction = float(_policy(policy, "stall_fraction", 0.25))
    return {
        "budget_seconds": int(math.ceil(budget)),
        "kill_at_seconds": int(math.ceil(kill_multiplier * budget)),
        "stall_seconds": int(math.ceil(stall_fraction * budget)),
        "source": source,
        "sample_count": len(selected),
        "widened": widened,
    }
