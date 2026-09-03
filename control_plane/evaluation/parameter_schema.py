"""ParameterSchema document contracts, canonical hashing, and candidate parameter validation."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from typing import Any

from control_plane.core.evaluation_contracts import (
    ContractError,
    canonical_json,
    normalize_token,
)


def _revision(value: Any, label: str) -> str:
    from control_plane.core.evaluation_contracts import _revision as core_rev
    return core_rev(value, label)


def _number(value: Any, label: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{label} must be numeric")
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractError(f"{label} must be finite")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractError(f"{label} must be a positive integer")
    return value


def validate_parameter_schema(value: Any) -> dict[str, Any]:
    """Validate and normalize a ParameterSchema document into its canonical structure."""
    if not isinstance(value, Mapping):
        raise ContractError("ParameterSchema must be an object")

    kind = value.get("kind")
    if kind != "parameter-schema":
        raise ContractError("ParameterSchema kind must be 'parameter-schema'")

    allowed_keys = {"kind", "problem_hint", "source_package", "parameters", "extracts"}
    unknown_keys = set(value) - allowed_keys
    if unknown_keys:
        raise ContractError(f"unknown ParameterSchema fields: {', '.join(sorted(unknown_keys))}")

    problem_hint = value.get("problem_hint")
    if problem_hint is not None:
        if not isinstance(problem_hint, str) or not problem_hint.strip():
            raise ContractError("problem_hint must be a non-empty string when provided")
        problem_hint = problem_hint.strip()

    source_pkg = value.get("source_package")
    normalized_source_pkg = None
    if source_pkg is not None:
        if not isinstance(source_pkg, Mapping) or set(source_pkg) != {"artifact_id", "revision"}:
            raise ContractError("source_package must contain exactly artifact_id and revision")
        normalized_source_pkg = {
            "artifact_id": normalize_token(source_pkg.get("artifact_id"), "source_package.artifact_id"),
            "revision": _revision(source_pkg.get("revision"), "source_package.revision"),
        }

    raw_params = value.get("parameters")
    if isinstance(raw_params, (str, bytes, bytearray)) or not isinstance(raw_params, Sequence):
        raise ContractError("parameters must be an array")
    if not raw_params:
        raise ContractError("parameters array must not be empty")

    normalized_parameters: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for idx, item in enumerate(raw_params):
        if not isinstance(item, Mapping):
            raise ContractError(f"parameters[{idx}] must be an object")
        label = f"parameters[{idx}]"
        name = normalize_token(item.get("name"), f"{label}.name")
        if name in seen_names:
            raise ContractError(f"duplicate parameter name '{name}' in schema")
        seen_names.add(name)

        param_type = str(item.get("type", "float")).strip().lower()
        if param_type not in {"float", "int"}:
            raise ContractError(f"{label}.type must be 'float' or 'int'")

        role = str(item.get("role", "")).strip().lower()
        if role not in {"variable", "fixed"}:
            raise ContractError(f"{label}.role must be 'variable' or 'fixed'")

        unit = item.get("unit")
        if unit is not None:
            if not isinstance(unit, str) or not unit.strip():
                raise ContractError(f"{label}.unit must be a non-empty string when provided")
            unit = unit.strip()

        deck_line = item.get("deck_line")
        if deck_line is not None:
            deck_line = _positive_int(deck_line, f"{label}.deck_line")

        if role == "variable":
            allowed_param_keys = {"name", "type", "role", "bounds", "default", "unit", "deck_line"}
            unknown_p = set(item) - allowed_param_keys
            if unknown_p:
                raise ContractError(f"{label} contains invalid fields for variable parameter: {', '.join(sorted(unknown_p))}")

            bounds = item.get("bounds")
            if not isinstance(bounds, Mapping) or set(bounds) != {"min", "max"}:
                raise ContractError(f"{label}.bounds must be an object containing min and max")
            min_val = _number(bounds.get("min"), f"{label}.bounds.min")
            max_val = _number(bounds.get("max"), f"{label}.bounds.max")
            if min_val > max_val:
                raise ContractError(f"{label}.bounds.min ({min_val}) cannot exceed max ({max_val})")

            default_val = item.get("default")
            if default_val is not None:
                default_val = _number(default_val, f"{label}.default")
                if default_val < min_val or default_val > max_val:
                    raise ContractError(f"{label}.default ({default_val}) must be within bounds [{min_val}, {max_val}]")

            param_doc: dict[str, Any] = {
                "name": name,
                "type": param_type,
                "role": "variable",
                "bounds": {"min": min_val, "max": max_val},
            }
            if default_val is not None:
                param_doc["default"] = default_val
            if unit is not None:
                param_doc["unit"] = unit
            if deck_line is not None:
                param_doc["deck_line"] = deck_line
            normalized_parameters.append(param_doc)

        else:  # role == "fixed"
            allowed_param_keys = {"name", "type", "role", "value", "unit", "deck_line"}
            unknown_p = set(item) - allowed_param_keys
            if unknown_p:
                raise ContractError(f"{label} contains invalid fields for fixed parameter: {', '.join(sorted(unknown_p))}")

            if "value" not in item:
                raise ContractError(f"{label} fixed parameter requires 'value'")
            val = _number(item.get("value"), f"{label}.value")

            param_doc = {
                "name": name,
                "type": param_type,
                "role": "fixed",
                "value": val,
            }
            if unit is not None:
                param_doc["unit"] = unit
            if deck_line is not None:
                param_doc["deck_line"] = deck_line
            normalized_parameters.append(param_doc)

    # Sort parameters deterministically by name for revision stability
    normalized_parameters.sort(key=lambda p: p["name"])

    raw_extracts = value.get("extracts")
    normalized_extracts: list[dict[str, Any]] | None = None
    if raw_extracts is not None:
        if isinstance(raw_extracts, (str, bytes, bytearray)) or not isinstance(raw_extracts, Sequence):
            raise ContractError("extracts must be an array")
        normalized_extracts = []
        seen_extract_names: set[str] = set()
        allowed_extract_keys = {"name", "expression", "line"}
        for idx, item in enumerate(raw_extracts):
            if not isinstance(item, Mapping):
                raise ContractError(f"extracts[{idx}] must be an object")
            label = f"extracts[{idx}]"
            unknown_e = set(item) - allowed_extract_keys
            if unknown_e:
                raise ContractError(f"{label} contains invalid fields: {', '.join(sorted(unknown_e))}")
            if "name" not in item:
                raise ContractError(f"{label} requires 'name'")
            name = normalize_token(item.get("name"), f"{label}.name")
            if name in seen_extract_names:
                raise ContractError(f"duplicate extract name '{name}' in schema")
            seen_extract_names.add(name)

            if "expression" not in item:
                raise ContractError(f"{label} requires 'expression'")
            expression = item.get("expression")
            if not isinstance(expression, str) or not expression.strip():
                raise ContractError(f"{label}.expression must be a non-empty string")
            expression = expression.strip()

            line = item.get("line")
            if line is not None:
                line = _positive_int(line, f"{label}.line")

            extract_doc: dict[str, Any] = {
                "name": name,
                "expression": expression,
            }
            if line is not None:
                extract_doc["line"] = line
            normalized_extracts.append(extract_doc)

        normalized_extracts.sort(key=lambda e: e["name"])

    canonical: dict[str, Any] = {
        "kind": "parameter-schema",
        "parameters": normalized_parameters,
    }
    if problem_hint is not None:
        canonical["problem_hint"] = problem_hint
    if normalized_source_pkg is not None:
        canonical["source_package"] = normalized_source_pkg
    if normalized_extracts is not None:
        canonical["extracts"] = normalized_extracts

    return canonical


def make_parameter_schema(
    *,
    parameters: Sequence[Mapping[str, Any]],
    problem_hint: str | None = None,
    source_package: Mapping[str, Any] | None = None,
    extracts: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Construct a canonical ParameterSchema dictionary."""
    body: dict[str, Any] = {
        "kind": "parameter-schema",
        "parameters": list(parameters),
    }
    if problem_hint is not None:
        body["problem_hint"] = problem_hint
    if source_package is not None:
        body["source_package"] = dict(source_package)
    if extracts is not None:
        body["extracts"] = list(extracts)
    return validate_parameter_schema(body)

def compute_schema_revision(document: Mapping[str, Any]) -> str:
    """Compute the stable SHA-256 revision hash for a canonical ParameterSchema document."""
    canonical = validate_parameter_schema(document)
    digest = hashlib.sha256(canonical_json(canonical).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def validate_parameters(
    schema: Mapping[str, Any], parameters: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate candidate parameters against a ParameterSchema document.

    Returns:
        {"valid": bool, "issues": [{"name": str, "code": str, "message": str}]}
    """
    canonical_schema = validate_parameter_schema(schema)
    if not isinstance(parameters, Mapping):
        raise ContractError("parameters must be an object")

    schema_params = {p["name"]: p for p in canonical_schema["parameters"]}
    issues: list[dict[str, Any]] = []

    # 1. Unknown parameters in candidate
    for name in parameters:
        if name not in schema_params:
            issues.append(
                {
                    "name": name,
                    "code": "unknown-parameter",
                    "message": f"parameter '{name}' is not declared in the schema",
                }
            )

    # 2. Missing variable parameters
    for p in canonical_schema["parameters"]:
        if p["role"] == "variable" and p["name"] not in parameters:
            issues.append(
                {
                    "name": p["name"],
                    "code": "missing-parameter",
                    "message": f"required parameter '{p['name']}' is missing",
                }
            )

    # 3. Fixed parameter overrides
    for p in canonical_schema["parameters"]:
        if p["role"] == "fixed" and p["name"] in parameters:
            val = parameters[p["name"]]
            if val != p["value"]:
                issues.append(
                    {
                        "name": p["name"],
                        "code": "fixed-parameter-override",
                        "message": (
                            f"fixed parameter '{p['name']}' cannot be modified "
                            f"(expected {p['value']}, got {val})"
                        ),
                    }
                )

    # 4. Type, finiteness, integer constraints, and bounds checking
    for name, val in parameters.items():
        if name in schema_params:
            spec = schema_params[name]
            if spec["role"] == "variable":
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    issues.append(
                        {
                            "name": name,
                            "code": "invalid-type",
                            "message": f"parameter '{name}' must be numeric, got {type(val).__name__}",
                        }
                    )
                    continue

                if isinstance(val, float) and not math.isfinite(val):
                    issues.append(
                        {
                            "name": name,
                            "code": "invalid-type",
                            "message": f"parameter '{name}' must be finite",
                        }
                    )
                    continue

                if spec.get("type") == "int" and isinstance(val, float) and not val.is_integer():
                    issues.append(
                        {
                            "name": name,
                            "code": "invalid-type",
                            "message": f"parameter '{name}' must be an integer, got {val}",
                        }
                    )
                    continue

                bounds = spec.get("bounds")
                if bounds is not None:
                    min_val = bounds["min"]
                    max_val = bounds["max"]
                    if val < min_val:
                        issues.append(
                            {
                                "name": name,
                                "code": "out-of-bounds",
                                "message": f"parameter '{name}' value {val} is below minimum {min_val}",
                            }
                        )
                    elif val > max_val:
                        issues.append(
                            {
                                "name": name,
                                "code": "out-of-bounds",
                                "message": f"parameter '{name}' value {val} exceeds maximum {max_val}",
                            }
                        )

    return {
        "valid": len(issues) == 0,
        "issues": issues,
    }


# Alias for compatibility
validate_candidate_parameters = validate_parameters
