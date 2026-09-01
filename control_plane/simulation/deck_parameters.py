"""TCAD deck parameter parsing and rewriting utilities."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping
from typing import Any

from control_plane.core.evaluation_contracts import ContractError

# Matches a Silvaco / TCAD parameter assignment line:
# set <name> = <raw_value>
_SET_LINE_RE = re.compile(
    r"^(?P<prefix>\s*set\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*)(?P<rest>.*)$",
    re.IGNORECASE,
)

# Number pattern matching integers, floats, scientific notation
_NUMERIC_RE = re.compile(
    r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$"
)

# Expression tokens for Silvaco TCAD decks
_EXPR_TOKEN_RE = re.compile(
    r"\s*(?:"
    r"(\$[A-Za-z_][A-Za-z0-9_]*)"  # variable reference: $var
    r"|(\b(?:sin|cos|tan|asin|acos|atan|exp|log|log10|sqrt|abs)\b)"  # standard math function
    r"|((?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)"  # number literal
    r"|([+\-*/^()])"  # arithmetic operator or parenthesis
    r")\s*"
)

# Matches an extract statement with name="<X>":
# extract name="<X>" <expr>
_EXTRACT_LINE_RE = re.compile(
    r'^\s*extract\s+name="(?P<name>[^"]+)"\s*(?P<expr>.*)$',
    re.IGNORECASE,
)


def _split_value_and_comment(rest: str) -> tuple[str, str, str]:
    """Split the remainder of a set line into value_str, separator_and_comment, and inline_comment.

    Returns:
        (value_part, trailing_part, inline_comment_text)
    """
    comment_idx = -1
    hash_idx = rest.find("#")
    if hash_idx != -1:
        comment_idx = hash_idx

    # Check for inline '$' comment: '$' preceded by whitespace (and not an operator)
    # and not part of an arithmetic expression
    dollar_match = re.search(r"(?<![+\-*/^,(])\s+(\$.*)$", rest)
    if dollar_match:
        d_idx = dollar_match.start(1)
        prefix = rest[:d_idx].rstrip()
        if prefix and prefix[-1] not in "+-*/^,(":
            if comment_idx == -1 or d_idx < comment_idx:
                comment_idx = d_idx

    if comment_idx != -1:
        val_part = rest[:comment_idx]
        trailing_part = rest[comment_idx:]
        comment_text = rest[comment_idx:].strip()
        return val_part.strip(), val_part[len(val_part.rstrip()):] + trailing_part, comment_text
    else:
        return rest.strip(), rest[len(rest.rstrip()):], ""


def _parse_numeric(val_str: str) -> float | int:
    """Parse a numeric string into int or float, ensuring it is finite."""
    if not _NUMERIC_RE.match(val_str):
        raise ValueError(f"malformed numeric value: {val_str!r}")
    try:
        if "." not in val_str and "e" not in val_str.lower():
            val: float | int = int(val_str)
        else:
            val = float(val_str)
    except ValueError as exc:
        raise ValueError(f"malformed numeric value: {val_str!r}") from exc
    if isinstance(val, float) and not math.isfinite(val):
        raise ValueError(f"numeric value is not finite: {val_str!r}")
    return val


def _is_valid_expression(val_str: str) -> bool:
    """Check whether a non-numeric string is a syntactically valid TCAD expression."""
    if not val_str or not val_str.strip():
        return False
    s_clean = val_str.strip()
    pos = 0
    has_operator_or_var = False
    while pos < len(s_clean):
        m = _EXPR_TOKEN_RE.match(s_clean, pos)
        if not m:
            return False
        # group 1 = variable, group 2 = function, group 4 = operator
        if m.group(1) or m.group(2) or m.group(4):
            has_operator_or_var = True
        pos = m.end()
    return has_operator_or_var


def parse_deck_parameters(deck_text: str) -> dict[str, Any]:
    """Parse parameter definitions and extract declarations from deck text.

    Returns a dict with:
      - parameters: list of dicts with keys name, type, kind, value, value_raw, line, unique
      - warnings: list of warning message strings
      - extracts: list of dicts with keys name, expression, line
    """
    if not isinstance(deck_text, str):
        raise ContractError("deck_text must be a string")

    lines = deck_text.splitlines()
    raw_params: list[dict[str, Any]] = []
    warnings: list[str] = []
    extracts: list[dict[str, Any]] = []

    for line_idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("$"):
            continue

        # Check for extract statements
        extract_match = _EXTRACT_LINE_RE.match(line)
        if extract_match:
            extracts.append(
                {
                    "name": extract_match.group("name"),
                    "expression": extract_match.group("expr").strip(),
                    "line": line_idx,
                }
            )
            continue
        if re.match(r"^\s*extract\b", line, re.IGNORECASE):
            # Non-naming extract statement (e.g. extract init ...) is ignored without warning
            continue

        match = _SET_LINE_RE.match(line)
        if not match:
            continue

        name = match.group("name")
        rest = match.group("rest")
        val_str, _, comment = _split_value_and_comment(rest)

        if comment:
            warnings.append(
                f"line {line_idx}: parameter '{name}' has inline comment: {comment}"
            )

        if not val_str:
            warnings.append(
                f"line {line_idx}: malformed set statement for '{name}': missing value"
            )
            continue

        if _NUMERIC_RE.match(val_str):
            try:
                num_val = _parse_numeric(val_str)
            except ValueError:
                warnings.append(
                    f"line {line_idx}: malformed numeric value for '{name}': {val_str}"
                )
                continue
            raw_params.append(
                {
                    "name": name,
                    "type": "numeric",
                    "kind": "numeric",
                    "value": num_val,
                    "value_raw": val_str,
                    "line": line_idx,
                    "unique": True,
                }
            )
        elif _is_valid_expression(val_str):
            raw_params.append(
                {
                    "name": name,
                    "type": "expression",
                    "kind": "expression",
                    "value": None,
                    "value_raw": val_str,
                    "line": line_idx,
                    "unique": True,
                }
            )
        else:
            warnings.append(
                f"line {line_idx}: malformed numeric value for '{name}': {val_str}"
            )
            continue

    # Detect duplicate parameter names
    name_counts = Counter(p["name"] for p in raw_params)
    for name, count in name_counts.items():
        if count > 1:
            matching_lines = [p["line"] for p in raw_params if p["name"] == name]
            warnings.append(
                f"duplicate set assignment for parameter '{name}' on lines: {', '.join(str(l) for l in matching_lines)}"
            )

    # Update uniqueness flag
    parameters: list[dict[str, Any]] = []
    for p in raw_params:
        parameters.append(
            {
                "name": p["name"],
                "type": p["type"],
                "kind": p["kind"],
                "value": p["value"],
                "value_raw": p["value_raw"],
                "line": p["line"],
                "unique": name_counts[p["name"]] == 1,
            }
        )

    return {
        "parameters": parameters,
        "warnings": warnings,
        "extracts": extracts,
    }


def rewrite_deck_parameters(
    deck_text: str, parameters: Mapping[str, Any]
) -> str:
    """Rewrite parameter values in deck text preserving formatting and whitespace.

    Uses python repr for full precision without truncation.
    Raises ContractError on unknown parameter, expression parameter, duplicate parameter,
    or non-numeric value.
    """
    if not isinstance(deck_text, str):
        raise ContractError("deck_text must be a string")
    if not isinstance(parameters, Mapping):
        raise ContractError("parameters must be a mapping")

    cleaned_params: dict[str, str] = {}
    for name, val in parameters.items():
        if not isinstance(name, str) or not name:
            raise ContractError("parameter name must be a non-empty string")
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ContractError(f"parameter '{name}' value must be numeric, got {type(val).__name__}")
        if isinstance(val, float) and not math.isfinite(val):
            raise ContractError(f"parameter '{name}' value must be finite")
        cleaned_params[name] = repr(val)

    if not cleaned_params:
        return deck_text

    parsed = parse_deck_parameters(deck_text)
    deck_param_map: dict[str, list[dict[str, Any]]] = {}
    for p in parsed["parameters"]:
        deck_param_map.setdefault(p["name"], []).append(p)

    for target_name in cleaned_params:
        if target_name not in deck_param_map:
            raise ContractError(f"unknown parameter: '{target_name}' not found in deck")
        occurrences = deck_param_map[target_name]
        if len(occurrences) > 1:
            lines_str = ", ".join(str(o["line"]) for o in occurrences)
            raise ContractError(
                f"cannot rewrite ambiguous duplicate parameter '{target_name}' found on lines {lines_str}"
            )
        param_entry = occurrences[0]
        if param_entry.get("type") == "expression" or param_entry.get("kind") == "expression":
            raise ContractError(
                f"cannot rewrite expression parameter '{target_name}'"
            )

    target_lines = {
        deck_param_map[name][0]["line"]: (name, cleaned_params[name])
        for name in cleaned_params
    }

    lines = deck_text.splitlines(keepends=True)
    rewritten_lines: list[str] = []

    for line_idx, line in enumerate(lines, start=1):
        if line_idx in target_lines:
            name, new_val_repr = target_lines[line_idx]
            stripped_content = line.rstrip("\r\n")
            newline = line[len(stripped_content):]
            match = _SET_LINE_RE.match(stripped_content)
            if not match:
                raise ContractError(f"failed to rewrite line {line_idx} for '{name}'")
            prefix = match.group("prefix")
            rest = match.group("rest")
            _, trailing, _ = _split_value_and_comment(rest)
            rewritten_lines.append(f"{prefix}{new_val_repr}{trailing}{newline}")
        else:
            rewritten_lines.append(line)

    return "".join(rewritten_lines)
