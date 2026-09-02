"""Reference worker for a long-lived solver connection + session token."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Mapping
from fake_server import FakeServer, ServerConnectionError, artifact_id
from control_plane.simulation.session_contracts import make_simulation_session_result, make_solver_run_record, normalize_session_ref

class _ResultWithRunRecord(dict):
    def __init__(self, result: Mapping[str, Any], run: Mapping[str, Any]) -> None:
        super().__init__(result)
        self._run = run

    def __getitem__(self, key: str) -> Any:
        return self._run if key == "solver_run_record" else super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        return self._run if key == "solver_run_record" else super().get(key, default)


class ServerSessionWorker:
    """Adapter whose durable identity is endpoint/token, not a disk job."""
    def __init__(self, server: FakeServer, state_path: str | Path | None = None) -> None:
        self.server = server
        self.state_path = None if state_path is None else Path(state_path)
        self._refs: dict[str, tuple[str, str]] = {}
        if self.state_path and self.state_path.is_file():
            self._refs = {k: tuple(v) for k, v in json.loads(self.state_path.read_text()).items()}
        self._connections: dict[str, dict[str, str]] = {}
        self._plans: dict[str, Mapping[str, Any]] = {}
        self._allocations: dict[str, Mapping[str, Any]] = {}
        self._terminated: set[str] = set()

    def _save(self) -> None:
        if self.state_path:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(self._refs, sort_keys=True))

    def start_session(self, plan: Mapping[str, Any], allocation: Mapping[str, Any], session_ref: str) -> str:
        # TODO 1: CONNECTION AND SESSION ESTABLISHMENT. How are service startup,
        # authentication, and session identity negotiated? Guessing wrong can
        # attach to another tenant or leak a license.
        ref = normalize_session_ref(session_ref)
        conn = self.server.connect()
        token = self.server.create_session(conn, ref)
        self._refs[ref] = (self.server.endpoint, token)
        self._connections[ref] = conn
        self._plans[ref], self._allocations[ref] = dict(plan), dict(allocation)
        self._save()
        return "running"

    def resume_session(self, plan: Mapping[str, Any], allocation: Mapping[str, Any], session_ref: str) -> None:
        ref = normalize_session_ref(session_ref)
        if ref not in self._refs:
            raise KeyError(ref)
        endpoint, token = self._refs[ref]
        conn = self.server.connect(token)
        self._connections[ref] = conn
        self._plans[ref], self._allocations[ref] = dict(plan), dict(allocation)
        # Reconnection is deliberately not create_session: repeated resume is idempotent.
        _ = endpoint

    def observe_session(self, session_ref: str) -> str:
        # TODO 2: PROGRESS AND SUCCESS CRITERIA. How does the service encode
        # running, completed, and divergent states? A bad mapping can turn a
        # failed solve into a false success (or report a live session absent).
        ref = normalize_session_ref(session_ref)
        if ref in self._terminated or ref not in self._refs:
            return "absent"
        try:
            return self.server.query(self._connections[ref], ref, self._refs[ref][1])
        except ServerConnectionError:
            return "unreachable"
        except KeyError:
            return "absent"

    def terminate_session(self, session_ref: str) -> str:
        ref = normalize_session_ref(session_ref)
        if ref not in self._refs or ref in self._terminated:
            return "terminated"
        try:
            self.server.disconnect(self._connections[ref], ref, self._refs[ref][1])
        except ServerConnectionError:
            # Explicit shutdown cannot be silently claimed after transport loss.
            return "unreachable"
        self._terminated.add(ref)
        return "terminated"

    def collect_session(self, session_ref: str) -> tuple[Mapping[str, Any], str]:
        # TODO 3: RESULT EXPORT. How is a remote result fetched and materialized
        # as an artifact? Guessing can produce unverifiable or incomplete evidence.
        ref = normalize_session_ref(session_ref)
        if self.observe_session(ref) != "completed":
            raise RuntimeError("session is not completed or reachable")
        exported = self.server.export(self._connections[ref], ref, self._refs[ref][1])
        # The service's structured solve field is the solve operation duration,
        # analogous to COMSOL's solve-time log entry, not session lifetime.
        try:
            raw_duration = exported["solve"]["elapsed_seconds"]
            duration = float(raw_duration) if raw_duration is not None and float(raw_duration) >= 0 else None
        except (KeyError, TypeError, ValueError):
            duration = None
        # TODO(adapter): map the real solver's log/response solve-time field here.
        if duration is None:
            # Missing server solve time is reported as None, never approximated.
            duration = None
        plan = self._plans[ref]
        run_id = str(self._allocations[ref].get("run_id", "server-run"))
        run = make_solver_run_record(
            plan_id=plan["plan_id"], sequence=1, run_id=run_id,
            package_artifact_id=plan["base_package"]["artifact_id"],
            package_revision=plan["base_package"]["revision"],
            numerical_profile_revision=plan["recovery_profile_revision"],
            action="server-session", status="completed", exit_code=0,
            artifact_ids=(artifact_id(exported),), wall_seconds=duration,
        )
        result = make_simulation_session_result(
            plan_id=plan["plan_id"], attempt_id=plan["attempt_id"], session_ref=ref,
            status="completed", solver_run_record_ids=(run["record_id"],),
            journal_artifact_id=artifact_id({"journal": ref}),
            evidence_artifact_ids=(artifact_id(exported),),
        )
        return _ResultWithRunRecord(result, run), artifact_id(exported)

# Friendly aliases used by readers migrating from basic-local.
SessionWorker = ServerSessionWorker
