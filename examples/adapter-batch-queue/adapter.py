"""Example worker for a submit-to-batch-queue execution model.

This is intentionally a fake scheduler adapter: no Slurm/LSF commands are run.
"""
from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Any, Mapping

from control_plane.simulation.session_contracts import make_simulation_session_result, make_solver_run_record
from control_plane.simulation.worker import SessionStartFailure, normalize_session_observation
try:
    from .fake_queue import FakeBatchQueue, QueueUnavailable
except ImportError:  # direct execution from run_demo.py
    from fake_queue import FakeBatchQueue, QueueUnavailable

BINDINGS_FILE = "batch-session-bindings.json"

def _parse_elapsed(value: str | None) -> float | None:
    """Parse sacct Elapsed semantics (DD-HH:MM:SS[.fraction])."""
    if not value:
        return None
    match = re.fullmatch(r"(?:(\d+)-)?(\d+):(\d+):(\d+(?:\.\d+)?)", value.strip())
    if not match:
        return None
    days, hours, minutes, seconds = match.groups()
    return int(days or 0) * 86400 + int(hours) * 3600 + int(minutes) * 60 + float(seconds)

class _ResultWithRunRecord(dict):
    def __init__(self, result: Mapping[str, Any], run: Mapping[str, Any]) -> None:
        super().__init__(result)
        self._run = run

    def __getitem__(self, key: str) -> Any:
        return self._run if key == "solver_run_record" else super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        return self._run if key == "solver_run_record" else super().get(key, default)


class BatchQueueWorker:
    def __init__(self, queue: FakeBatchQueue | None = None) -> None:
        self.queue = queue or FakeBatchQueue()
        self._bindings: dict[str, str] = {}
        self._plans: dict[str, Mapping[str, Any]] = {}
        self._allocations: dict[str, Mapping[str, Any]] = {}

    def _binding_path(self, allocation: Mapping[str, Any]) -> Path:
        root = allocation.get("remote_workspace_root")
        if not root:
            raise ValueError("allocation.remote_workspace_root is required")
        path = Path(str(root)); path.mkdir(parents=True, exist_ok=True)
        return path / BINDINGS_FILE

    def _load(self, path: Path) -> dict[str, str]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return dict(value) if isinstance(value, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _save(self, path: Path, values: Mapping[str, str]) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(values), sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)

    def start_session(self, plan: Mapping[str, Any], allocation: Mapping[str, Any], session_ref: str) -> None:
        ref = str(session_ref)
        try:
            path = self._binding_path(allocation)
        except Exception as exc:
            raise SessionStartFailure("preflight_failed", "deterministic", str(exc)) from exc
        if not plan.get("plan_id") or not plan.get("attempt_id"):
            raise SessionStartFailure("preflight_failed", "deterministic", "plan_id and attempt_id are required")
        existing = self._load(path)
        if ref in existing:
            self._bindings[ref] = existing[ref]
            self._plans[ref], self._allocations[ref] = plan, allocation
            return
        try:
            # TODO(submission command): decide how this software is invoked and whether
            # processors belong on its CLI or in an input/deck file. Wrong choice can
            # silently run with one core or alter the wrong simulation.
            job_id = self.queue.submit({"session_ref": ref, "processors": allocation.get("processors", 1)})
        except QueueUnavailable as exc:
            raise SessionStartFailure("indeterminate", "transport", str(exc)) from exc
        existing[ref] = job_id
        try:
            self._save(path, existing)  # durable before returning to caller
        except OSError as exc:
            raise SessionStartFailure("indeterminate", "persistence", "job submitted but binding was not persisted") from exc
        self._bindings[ref] = job_id
        self._plans[ref], self._allocations[ref] = plan, allocation

    def resume_session(self, plan: Mapping[str, Any], allocation: Mapping[str, Any], session_ref: str) -> None:
        ref = str(session_ref)
        if ref not in self._bindings:
            self._bindings.update(self._load(self._binding_path(allocation)))
        if ref not in self._bindings:
            raise SessionStartFailure("indeterminate", "binding", f"no durable binding for {ref}")
        self._plans[ref], self._allocations[ref] = plan, allocation

    def observe_session(self, session_ref: str) -> str:
        ref = str(session_ref); job_id = self._bindings.get(ref)
        if not job_id:
            return "indeterminate"
        try:
            active = self.queue.query_active(job_id)
            if active is not None:
                return "running"
            history = self.queue.query_history(job_id)
        except QueueUnavailable:
            return "unreachable"
        if history is None:
            return "indeterminate"
        if history.state == "COMPLETED" and history.exit_code == 0:
            # TODO(success criterion): locate this solver's convergence/divergence
            # marker in its output, not merely queue COMPLETED/exit 0. Wrong choice
            # reports incomplete numerical results as completed.
            return "completed"
        return "indeterminate"

    def terminate_session(self, session_ref: str) -> str:
        job_id = self._bindings.get(str(session_ref))
        if not job_id:
            return "absent"
        try:
            self.queue.cancel(job_id)
            active = self.queue.query_active(job_id)
            history = self.queue.query_history(job_id)
        except QueueUnavailable:
            return "unreachable"
        if active is not None:
            return "indeterminate"
        if history is not None and history.state in {"CANCELLED", "COMPLETED", "FAILED"}:
            return "terminated"
        return "absent"

    def collect_session(self, session_ref: str) -> tuple[Mapping[str, Any], str]:
        ref = str(session_ref); plan = self._plans.get(ref, {})
        job_id = self._bindings.get(ref)
        if not job_id:
            raise RuntimeError(f"unknown session {ref}")
        history = self.queue.query_history(job_id)
        if history is None:
            raise RuntimeError("session history is unavailable")
        exit_code = history.exit_code
        completed = history.state == "COMPLETED" and exit_code == 0
        package = plan.get("base_package", {"artifact_id": "artifact.batch-package", "revision": "unknown"})
        run = make_solver_run_record(plan_id=plan["plan_id"], sequence=1, run_id=job_id,
            package_artifact_id=package["artifact_id"], package_revision=package["revision"],
            numerical_profile_revision=plan.get("recovery_profile_revision", "batch-profile"),
            action="initial", status="completed" if completed else "failed", exit_code=exit_code,
            artifact_ids=[f"artifact.batch.{job_id}"] if completed else [],
            wall_seconds=_parse_elapsed(history.elapsed))
        # TODO(adapter): invoke sacct and define its exact --format output for real Slurm.
        # artifacts. Wrong extraction can omit convergence evidence or expose stale data.
        result = make_simulation_session_result(plan_id=plan["plan_id"], attempt_id=plan["attempt_id"],
            session_ref=ref, status="completed" if completed else "indeterminate",
            solver_run_record_ids=[run["record_id"]], journal_artifact_id=f"artifact.batch.journal.{job_id}",
            evidence_artifact_ids=[f"artifact.batch.{job_id}"] if completed else [],
            terminal_cause=None if completed else "queue-nonzero-exit")
        return _ResultWithRunRecord(result, run), f"artifact.batch.{job_id}"

    def job_id_for(self, session_ref: str) -> str | None:
        return self._bindings.get(str(session_ref))
