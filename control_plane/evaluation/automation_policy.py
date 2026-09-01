"""Fail-closed project policy for evaluation recovery automation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


DEFAULT_AUTOMATION_POLICY: dict[str, Any] = {
    "platform": {"requeue_limit": 2, "tier1_min_samples": 20, "tier2_min_samples": 30},
    "profiles": {
        "autonomous": {"timeout_transient_rerun": True, "pathological_point": "skip-and-mark"},
        "assisted": {"timeout_transient_rerun": True, "pathological_point": "report-and-wait"},
        "manual": {"timeout_transient_rerun": False, "pathological_point": "report-and-wait"},
    },
    "default_profile": "assisted",
}
_PROFILE_NAMES = frozenset(DEFAULT_AUTOMATION_POLICY["profiles"])
_PATHOLOGICAL = frozenset({"skip-and-mark", "report-and-wait"})


class AutomationPolicyError(ValueError):
    """Raised when AUTOMATION_POLICY.json is present but invalid."""


class AutomationPolicyBlocked(AutomationPolicyError):
    """Expected fail-closed policy configuration error."""


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AutomationPolicyError(f"{label} must be a positive integer")
    return value


def validate_automation_policy(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the complete, deliberately small policy schema."""
    if not isinstance(value, Mapping) or set(value) != {"platform", "profiles", "default_profile"}:
        raise AutomationPolicyError("AutomationPolicy contains missing or unknown fields")
    platform = value["platform"]
    if not isinstance(platform, Mapping) or set(platform) != {"requeue_limit", "tier1_min_samples", "tier2_min_samples"}:
        raise AutomationPolicyError("AutomationPolicy platform is invalid")
    normalized_platform = {key: _positive(platform[key], f"platform.{key}") for key in sorted(platform)}
    if normalized_platform["tier2_min_samples"] < normalized_platform["tier1_min_samples"]:
        raise AutomationPolicyError("platform.tier2_min_samples must be at least tier1_min_samples")
    profiles = value["profiles"]
    if not isinstance(profiles, Mapping) or set(profiles) != _PROFILE_NAMES:
        raise AutomationPolicyError("AutomationPolicy profiles are invalid")
    normalized_profiles: dict[str, dict[str, Any]] = {}
    for name in sorted(_PROFILE_NAMES):
        profile = profiles[name]
        if not isinstance(profile, Mapping) or set(profile) != {"timeout_transient_rerun", "pathological_point"}:
            raise AutomationPolicyError(f"AutomationPolicy profile {name} is invalid")
        rerun = profile["timeout_transient_rerun"]
        if not isinstance(rerun, bool):
            raise AutomationPolicyError(f"profiles.{name}.timeout_transient_rerun must be boolean")
        pathological = profile["pathological_point"]
        if not isinstance(pathological, str) or pathological not in _PATHOLOGICAL:
            raise AutomationPolicyError(f"profiles.{name}.pathological_point is invalid")
        normalized_profiles[name] = {"timeout_transient_rerun": rerun, "pathological_point": pathological}
    default = value["default_profile"]
    if not isinstance(default, str) or default not in _PROFILE_NAMES:
        raise AutomationPolicyError("default_profile must reference an existing profile")
    normalized = {"platform": normalized_platform, "profiles": normalized_profiles, "default_profile": default}
    # Reject values which only become acceptable after coercion (notably 1.0).
    if dict(value) != normalized:
        raise AutomationPolicyError("AutomationPolicy contains invalid values")
    return json.loads(json.dumps(normalized, sort_keys=True, separators=(",", ":")))


def resolve_automation_policy(project_root: Path | str) -> dict[str, Any]:
    """Load project/AUTOMATION_POLICY.json, or return the built-in default."""
    path = Path(project_root).resolve() / "project" / "AUTOMATION_POLICY.json"
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_AUTOMATION_POLICY, sort_keys=True, separators=(",", ":")))
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AutomationPolicyBlocked("cannot read AUTOMATION_POLICY") from exc
    try:
        return validate_automation_policy(raw)
    except AutomationPolicyError as exc:
        raise AutomationPolicyBlocked(str(exc)) from exc


def profile_rank(profile: str) -> int:
    """Return conservative ordering: manual < assisted < autonomous."""
    return {"manual": 0, "assisted": 1, "autonomous": 2}[profile]


def most_conservative(profiles: list[str], default: str) -> str:
    return min(profiles or [default], key=profile_rank)
