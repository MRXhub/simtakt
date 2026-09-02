"""Explicit, fail-closed loader for project simulation adapters."""
from __future__ import annotations
import importlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from control_plane.simulation.adapter_protocol import PLATFORM_ADAPTER_INTERFACE_VERSIONS


class AdapterCatalogError(RuntimeError):
    """Raised when a project adapter catalog or binding is invalid."""


@dataclass(frozen=True)
class ResolvedAdapter:
    adapter_id: str
    status: str
    interface_version: int
    adapter: Any
    entry: Mapping[str, Any]


def _catalog_error(path: str, message: str, adapter_id: str | None = None) -> AdapterCatalogError:
    suffix = f" (adapter_id {adapter_id})" if adapter_id else ""
    return AdapterCatalogError(f"{path}{suffix}: {message}")


def load_catalog(project_root: Path | str) -> list[Mapping[str, Any]]:
    """Read and validate the explicit project registration document."""

    path = Path(project_root).resolve() / "project" / "SIMULATION_ADAPTERS.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _catalog_error("catalog", "cannot read catalog") from exc

    if not isinstance(document, Mapping):
        raise _catalog_error("catalog", "expected object")
    schema_version = document.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise _catalog_error("schema_version", "expected non-boolean integer")
    if schema_version != 1:
        raise _catalog_error("schema_version", "expected 1")

    catalog_id = document.get("catalog_id")
    if not isinstance(catalog_id, str):
        raise _catalog_error("catalog_id", "expected string")
    if catalog_id != "simulation-adapters":
        raise _catalog_error("catalog_id", "expected simulation-adapters")

    entries = document.get("adapters")
    if not isinstance(entries, list):
        raise _catalog_error("adapters", "expected array")

    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    required = (
        "adapter_id",
        "status",
        "module",
        "factory",
        "interface_version",
        "capabilities",
        "resource_defaults",
    )
    for index, entry in enumerate(entries):
        entry_path = f"adapters[{index}]"
        if not isinstance(entry, Mapping):
            raise _catalog_error(entry_path, "expected object")

        raw_id = entry.get("adapter_id")
        adapter_id = raw_id if isinstance(raw_id, str) and raw_id.strip() else None
        for field in required:
            if field not in entry:
                raise _catalog_error(f"{entry_path}.{field}", "required", adapter_id)

        if adapter_id is None:
            raise _catalog_error(
                f"{entry_path}.adapter_id", "expected non-empty string", adapter_id
            )
        if adapter_id in seen:
            raise _catalog_error(
                f"{entry_path}.adapter_id", f"duplicate adapter_id {adapter_id}", adapter_id
            )
        seen.add(adapter_id)

        status = entry.get("status")
        if not isinstance(status, str):
            raise _catalog_error(
                f"{entry_path}.status", "expected string", adapter_id
            )
        if status not in {"active", "experimental", "disabled"}:
            raise _catalog_error(
                f"{entry_path}.status",
                "expected active, experimental, or disabled",
                adapter_id,
            )

        for field in ("module", "factory"):
            value = entry.get(field)
            if not isinstance(value, str) or not value:
                raise _catalog_error(
                    f"{entry_path}.{field}", "expected non-empty string", adapter_id
                )

        interface_version = entry.get("interface_version")
        if isinstance(interface_version, bool) or not isinstance(interface_version, int):
            raise _catalog_error(
                f"{entry_path}.interface_version",
                "expected non-boolean integer",
                adapter_id,
            )
        if interface_version not in PLATFORM_ADAPTER_INTERFACE_VERSIONS:
            raise _catalog_error(
                f"{entry_path}.interface_version",
                f"expected one of {sorted(PLATFORM_ADAPTER_INTERFACE_VERSIONS)}",
                adapter_id,
            )
        capabilities = entry.get("capabilities")
        if (
            isinstance(capabilities, (str, bytes, bytearray))
            or not isinstance(capabilities, list)
            or not capabilities
            or any(not isinstance(cap, str) or not cap.strip() for cap in capabilities)
            or len(set(capabilities)) != len(capabilities)
        ):
            raise _catalog_error(
                f"{entry_path}.capabilities",
                "expected non-empty array of unique non-empty strings",
                adapter_id,
            )
        resource_defaults = entry.get("resource_defaults")
        if not isinstance(resource_defaults, Mapping):
            raise _catalog_error(
                f"{entry_path}.resource_defaults", "expected object", adapter_id
            )
        required_resources = {"processors", "memory_bytes", "max_wall_seconds"}
        if not required_resources.issubset(resource_defaults):
            raise _catalog_error(
                f"{entry_path}.resource_defaults",
                "required processors, memory_bytes, and max_wall_seconds",
                adapter_id,
            )
        for resource, value in resource_defaults.items():
            if (
                not isinstance(resource, str)
                or not resource.strip()
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise _catalog_error(
                    f"{entry_path}.resource_defaults",
                    "values must be finite non-negative numbers",
                    adapter_id,
                )
    return result


def resolve_adapter(project_root: Path | str, adapter_id: str) -> ResolvedAdapter:
    entries = load_catalog(project_root)
    entry = next((item for item in entries if item.get("adapter_id") == adapter_id), None)
    if entry is None:
        raise AdapterCatalogError(f"adapter_id {adapter_id}: not registered")
    if entry["status"] == "disabled":
        raise AdapterCatalogError(
            f"adapter_id {adapter_id} status: expected active or experimental, got disabled"
        )
    try:
        module = importlib.import_module(entry["module"])
    except Exception as exc:
        raise AdapterCatalogError(
            f"adapter_id {adapter_id} module: import failed for {entry['module']}"
        ) from exc
    factory = getattr(module, entry["factory"], None)
    if not callable(factory):
        raise AdapterCatalogError(
            f"adapter_id {adapter_id} factory: expected callable {entry['factory']}"
        )
    try:
        adapter = factory(entry)
    except Exception as exc:
        raise AdapterCatalogError(
            f"adapter_id {adapter_id} factory: construction failed"
        ) from exc
    for name in ("adapter_id", "build_gateway", "materialize_package", "validate_package", "qualify"):
        if not hasattr(adapter, name) or (
            name != "adapter_id" and not callable(getattr(adapter, name))
        ):
            raise AdapterCatalogError(
                f"adapter_id {adapter_id} {name}: required protocol binding"
            )
    if adapter.adapter_id != adapter_id:
        raise AdapterCatalogError(
            f"adapter_id {adapter_id} adapter_id: expected {adapter_id}"
        )
    return ResolvedAdapter(
        adapter_id,
        entry["status"],
        entry["interface_version"],
        adapter,
        entry,
    )


def resolve_project_adapters(project_root: Path | str) -> dict[str, ResolvedAdapter]:
    """Resolve only runnable (active or experimental) registrations."""

    return {
        entry["adapter_id"]: resolve_adapter(project_root, entry["adapter_id"])
        for entry in load_catalog(project_root)
        if entry["status"] in {"active", "experimental"}
    }


def resolve_adapter_for_problem(
    project_root: Path | str, problem: Mapping[str, Any]
) -> ResolvedAdapter:
    """Resolve the sole runnable adapter (active or experimental) covering a problem."""
    capabilities = problem.get("simulation_capabilities")
    if isinstance(capabilities, (str, bytes, bytearray)) or not isinstance(
        capabilities, list
    ):
        raise AdapterCatalogError("problem simulation_capabilities: expected array")
    required = set(capabilities)
    entries = load_catalog(project_root)
    runnable = [
        entry for entry in entries if entry["status"] in {"active", "experimental"}
    ]
    considered = sorted(str(entry["adapter_id"]) for entry in runnable)
    matches = [
        entry for entry in runnable
        if required.issubset(set(entry["capabilities"]))
    ]
    required_text = ", ".join(sorted(str(cap) for cap in required))
    considered_text = ", ".join(considered) or "(none)"
    if not matches:
        raise AdapterCatalogError(
            "problem simulation_capabilities: no matching adapter "
            f"(required: [{required_text}], considered: [{considered_text}])"
        )
    if len(matches) > 1:
        matching_text = ", ".join(
            sorted(str(entry["adapter_id"]) for entry in matches)
        )
        raise AdapterCatalogError(
            "problem simulation_capabilities: multiple matching adapters "
            f"(matches: [{matching_text}])"
        )
    return resolve_adapter(project_root, str(matches[0]["adapter_id"]))
