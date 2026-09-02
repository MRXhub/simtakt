"""Build the governed control-plane runtime from project declarations."""
from __future__ import annotations

import importlib
import json
import os
import secrets
import socket
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from control_plane.data.sqlite_evaluation_repository import SQLiteEvaluationRepository
from control_plane.evaluation.control_plane import resolve_control_plane_database
from control_plane.evaluation.execution_topology import ProjectFileTargetCatalog, parse_execution_topology
from control_plane.evaluation.preparation_phase import PreparationPhase
from control_plane.evaluation.project_ports import ProjectFileControlStore
from control_plane.evaluation.scheduling_policy import resolve_governed_scheduling_policy
from control_plane.evaluation.service import EvaluationMiddleware


class RuntimeCompositionError(RuntimeError):
    """Raised when a runtime cannot be safely assembled."""


@dataclass
class RuntimeContext:
    project_root: Path
    middleware: Any
    dispatcher: Any
    worker: Any
    resource_monitor: Any
    components: dict[str, Any]
    control_store: Any
    target_catalog: Any
    execution_topology: Mapping[str, Any]
    exit_stack: ExitStack
    assembly_summary: dict[str, Any]

    def close(self) -> None:
        self.exit_stack.close()


def _read_components(root: Path) -> list[Mapping[str, Any]]:
    path = root / "project" / "RUNTIME_COMPONENTS.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise RuntimeCompositionError(f"missing project/RUNTIME_COMPONENTS.json (required fields: schema_version, components)") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeCompositionError("project/RUNTIME_COMPONENTS.json is not valid JSON") from exc
    if not isinstance(document, Mapping):
        raise RuntimeCompositionError("project/RUNTIME_COMPONENTS.json must be a JSON object")
    if document.get("schema_version") != 1:
        raise RuntimeCompositionError("RUNTIME_COMPONENTS.json.schema_version must be 1")
    raw = document.get("components")
    if isinstance(raw, Mapping):
        entries = [dict(value, name=name) if isinstance(value, Mapping) else value for name, value in raw.items()]
    elif isinstance(raw, list):
        entries = raw
    else:
        raise RuntimeCompositionError("RUNTIME_COMPONENTS.json.components must be an object or array")
    result: list[Mapping[str, Any]] = []
    names: set[str] = set()
    for item in entries:
        if not isinstance(item, Mapping):
            raise RuntimeCompositionError("RUNTIME_COMPONENTS.json.components entries must be objects")
        name = item.get("name", item.get("component"))
        if not isinstance(name, str) or not name.strip():
            raise RuntimeCompositionError("RUNTIME_COMPONENTS.json component missing name")
        name = name.strip()
        if name in names:
            raise RuntimeCompositionError(f"duplicate runtime component {name}")
        names.add(name)
        for field in ("module", "factory", "interface_version"):
            if field not in item:
                raise RuntimeCompositionError(f"runtime component {name} missing field {field}")
        if not isinstance(item["module"], str) or not item["module"] or not isinstance(item["factory"], str) or not item["factory"]:
            raise RuntimeCompositionError(f"runtime component {name} module and factory must be non-empty strings")
        if not isinstance(item["interface_version"], int) or isinstance(item["interface_version"], bool):
            raise RuntimeCompositionError(f"runtime component {name} interface_version must be an integer")
        result.append(dict(item, name=name))
    for required in ("worker", "resource_monitor"):
        if not any(item.get("name") == required for item in result):
            raise RuntimeCompositionError(f"RUNTIME_COMPONENTS.json missing required component {required}")
    return result


def _load(entry: Mapping[str, Any]) -> Any:
    name = str(entry["name"]); ref = f"{entry['module']}:{entry['factory']}"
    try:
        module = importlib.import_module(str(entry["module"]))
    except Exception as exc:
        raise RuntimeCompositionError(f"component {name} module import failed ({ref})") from exc
    factory = getattr(module, str(entry["factory"]), None)
    if not callable(factory):
        raise RuntimeCompositionError(f"component {name} factory is not callable ({ref})")
    try:
        return factory(entry)
    except Exception as exc:
        raise RuntimeCompositionError(f"component {name} factory construction failed ({ref})") from exc


def _check(name: str, obj: Any, required: tuple[str, ...], entry: Mapping[str, Any]) -> None:
    ref = f"{entry['module']}:{entry['factory']}"
    for method in required:
        if not callable(getattr(obj, method, None)):
            raise RuntimeCompositionError(f"component {name} missing required method {method} ({ref})")
def _dispatcher_id(root: Path, entries: list[Mapping[str, Any]]) -> str:
    """Resolve an explicit dispatcher identity or generate a process-unique one."""
    configured: Any = None
    try:
        document = json.loads(
            (root / "project" / "RUNTIME_COMPONENTS.json").read_text(
                encoding="utf-8-sig"
            )
        )
        if isinstance(document, Mapping):
            configured = document.get("dispatcher_id")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    if configured is None:
        for entry in entries:
            if entry.get("name") == "dispatcher" and entry.get("dispatcher_id") is not None:
                configured = entry["dispatcher_id"]
                break
    if configured is not None:
        value = str(configured).strip()
        if value:
            return value
        raise RuntimeCompositionError("dispatcher_id override must be non-empty")
    return f"runtime:{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"



def compose_runtime(project_root: Path | str) -> RuntimeContext:
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise RuntimeCompositionError(f"project root is not an existing directory: {root}")
    entries = _read_components(root)
    loaded: dict[str, Any] = {}
    stack = ExitStack()
    summary: dict[str, Any] = {}
    try:
        for entry in entries:
            name = str(entry["name"])
            obj = _load(entry)
            required = (
                ("start_session", "resume_session", "observe_session", "collect_session")
                if name == "worker" else
                (("locked_snapshot", "record_decision") if name == "resource_monitor" else ())
            )
            _check(name, obj, required, entry)
            close = getattr(obj, "close", None)
            if callable(close):
                stack.callback(close)
            if callable(getattr(obj, "open", None)):
                obj.open()
            loaded[name] = obj
            summary[name] = {
                "termination": "confirmed"
                if callable(getattr(obj, "terminate_session", None))
                else "无法确认终止"
            }
        control_store = ProjectFileControlStore()
        target_catalog = ProjectFileTargetCatalog()
        topology = parse_execution_topology(root, target_catalog)
        if len(topology.get("formal_target_ids", ())) > 1 and not callable(
            getattr(loaded["resource_monitor"], "locked_dispatch", None)
        ):
            raise RuntimeCompositionError(
                "component resource_monitor missing required method locked_dispatch for multiple targets"
            )
        repository = SQLiteEvaluationRepository(resolve_control_plane_database(root))
        repository.target_catalog = target_catalog
        middleware = EvaluationMiddleware(
            repository, project_root=root, control_store=control_store
        )
        policy = resolve_governed_scheduling_policy(root, control_store=control_store)
        governance = lambda preparation, now=None: validate_policy_derived_execution_preparation(
            root, preparation, now=now, control_store=control_store,
            target_catalog=target_catalog
        )
        dispatcher = PreparedExecutionDispatcher(
            middleware, loaded["resource_monitor"], loaded["worker"],
            dispatcher_id=_dispatcher_id(root, entries), lease_seconds=60,
            preparation_governance=governance, scheduling_policy=policy,
            execution_topology=topology,
        )
        dispatcher.preparer = PreparationPhase(repository, root, window_limit=1)
        return RuntimeContext(
            root, middleware, dispatcher, loaded["worker"],
            loaded["resource_monitor"], loaded, control_store, target_catalog,
            topology, stack, summary
        )
    except Exception as exc:
        stack.close()
        if isinstance(exc, RuntimeCompositionError):
            raise
        raise RuntimeCompositionError(f"runtime assembly failed: {exc}") from exc


# concise alias
compose = compose_runtime
