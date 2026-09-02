"""Minimal fixed-quota monitor and fake worker for runtime examples.

Real deployments should replace the monitor with an adapter querying local
CPU/memory, the site's scheduler (such as Slurm), and the approved license
service. TODO(adapter): integrate authoritative site-specific APIs.
"""
from __future__ import annotations
import json, os, time, uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

class FixedQuotaResourceMonitor:
    def __init__(self, entry: Mapping[str, Any]):
        cfg = entry.get("config", {})
        self.root = Path(cfg.get("runtime_dir", "runtime")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.quota = {"processors": int(cfg.get("processors", 4)), "memory_bytes": int(cfg.get("memory_bytes", 1)), "license_sessions": int(cfg.get("license_sessions", 2))}
        self.lock_path = self.root / "resource.lock"
        self.receipts = self.root / "decisions"
        self.receipts.mkdir(parents=True, exist_ok=True)

    def open(self) -> None: self.receipts.mkdir(parents=True, exist_ok=True)
    def close(self) -> None: pass

    @contextmanager
    def _lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd); break
            except FileExistsError:
                time.sleep(0.005)
        try: yield
        finally:
            try: self.lock_path.unlink()
            except FileNotFoundError: pass

    @contextmanager
    def locked_snapshot(self, target_id: str):
        with self._lock(): yield {**self.quota, "target_id": target_id}

    @contextmanager
    def locked_dispatch(self):
        with self._lock(): yield

    def record_decision(self, decision: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]], active_allocations: Sequence[Mapping[str, Any]], resource_snapshot: Mapping[str, Any], **kwargs: Any) -> tuple[str, Path]:
        artifact_id = "runtime-decision-" + uuid.uuid4().hex
        path = self.receipts / (artifact_id + ".json")
        payload = {"artifact_id": artifact_id, "decision": dict(decision), "candidates": list(candidates), "active_allocations": list(active_allocations), "resource_snapshot": dict(resource_snapshot), "recorded_at": datetime.now(timezone.utc).isoformat()}
        path.write_text(json.dumps(payload, sort_keys=True, default=str), encoding="utf-8")
        return artifact_id, path

class MinimalWorker:
    def __init__(self, entry: Mapping[str, Any]): self.sessions: dict[str, str] = {}; self.delay = float(entry.get("config", {}).get("delay_seconds", 0.001))
    def start_session(self, plan: Mapping[str, Any], allocation: Mapping[str, Any], session_ref: str) -> None:
        if session_ref in self.sessions: return
        self.sessions[session_ref] = "running"
        if self.delay: time.sleep(self.delay)
    def resume_session(self, plan: Mapping[str, Any], allocation: Mapping[str, Any], session_ref: str) -> None:
        self.sessions.setdefault(session_ref, "running")
    def observe_session(self, session_ref: str) -> str: return self.sessions.get(session_ref, "absent")
    def collect_session(self, session_ref: str) -> tuple[Mapping[str, Any], str]:
        self.sessions[session_ref] = "completed"; return {"session_ref": session_ref, "status": "completed"}, "completed"
    def terminate_session(self, session_ref: str) -> str: self.sessions[session_ref] = "terminated"; return "terminated"

def resource_monitor_factory(entry: Mapping[str, Any]) -> FixedQuotaResourceMonitor: return FixedQuotaResourceMonitor(entry)
def worker_factory(entry: Mapping[str, Any]) -> MinimalWorker: return MinimalWorker(entry)
