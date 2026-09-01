"""Deterministic in-memory queue with active (squeue) and history (sacct) views."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

class QueueUnavailable(RuntimeError):
    """Scheduler cannot be contacted."""

@dataclass
class Job:
    job_id: str
    payload: dict[str, Any]
    state: str = "RUNNING"
    exit_code: int | None = None

class FakeBatchQueue:
    def __init__(self) -> None:
        self.active: dict[str, Job] = {}
        self.history: dict[str, Job] = {}
        self.submit_count = 0
        self._next = 1
        self.fail_queries = False
        self.fail_submit = False
        self.fail_cancel = False
        self.history_retention = True

    def submit(self, payload: dict[str, Any]) -> str:
        if self.fail_submit:
            raise QueueUnavailable("scheduler unavailable during submit")
        job_id = f"fake-{self._next}"
        self._next += 1
        self.submit_count += 1
        self.active[job_id] = Job(job_id, dict(payload))
        return job_id

    def query_active(self, job_id: str) -> Job | None:
        if self.fail_queries:
            raise QueueUnavailable("scheduler unavailable (active query)")
        return self.active.get(str(job_id))

    def query_history(self, job_id: str) -> Job | None:
        if self.fail_queries:
            raise QueueUnavailable("scheduler unavailable (history query)")
        if not self.history_retention:
            return None
        return self.history.get(str(job_id))

    def cancel(self, job_id: str) -> None:
        if self.fail_cancel:
            raise QueueUnavailable("scheduler unavailable during cancel")
        job = self.active.pop(str(job_id), None)
        if job is not None:
            job.state, job.exit_code = "CANCELLED", 130
            self.history[job.job_id] = job

    def complete(self, job_id: str, exit_code: int = 0) -> None:
        job = self.active.pop(str(job_id), None)
        if job is None:
            raise KeyError(job_id)
        job.state, job.exit_code = ("COMPLETED" if exit_code == 0 else "FAILED"), int(exit_code)
        self.history[job.job_id] = job

    def expire_history(self, job_id: str | None = None) -> None:
        if job_id is None:
            self.history.clear()
        else:
            self.history.pop(str(job_id), None)
