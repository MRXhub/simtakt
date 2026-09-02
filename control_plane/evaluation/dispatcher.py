"""Coordinate queue state, pure scheduling, and Worker lifecycle."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from control_plane.core.evaluation_contracts import ContractError
from typing import Any, Protocol

from control_plane.evaluation.service import EvaluationMiddleware
from control_plane.evaluation.scheduling import schedule
from control_plane.simulation.worker import (
    SimulationWorker,
    normalize_session_observation,
    normalize_session_termination,
)
from control_plane.simulation.gateway import ReceiptIntegrityError


class DispatchError(RuntimeError):
    """Raised when scheduling or Worker lifecycle breaks the dispatch contract."""


class ResourceMonitor(Protocol):
    """Supply fresh target facts and persist a decision receipt."""

    def locked_snapshot(
        self, target_id: str
    ) -> AbstractContextManager[dict[str, Any]]: ...

    def record_decision(
        self,
        decision: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
        active_allocations: Sequence[Mapping[str, Any]],
        resource_snapshot: Mapping[str, Any],
        *,
        scheduling_policy: Mapping[str, Any] | None = None,
        decision_time: datetime | str | None = None,
        capacity_envelope: Mapping[str, Any] | None = None,
        capacity_profile_snapshot: Mapping[str, Any] | None = None,
        task_classes: Sequence[Mapping[str, Any]] = (),
        overrides: Sequence[Mapping[str, Any]] = (),
        scheduling_policy_provenance: Mapping[str, Any] | None = None,
    ) -> tuple[str, Path]: ...


class SessionLifecycleDispatcher:
    """Recover, observe, and collect an already allocated Session."""

    def __init__(
        self,
        middleware: EvaluationMiddleware,
        resource_monitor: ResourceMonitor,
        worker: SimulationWorker,
        *,
        dispatcher_id: str,
        lease_seconds: int,
        scheduler: Callable[
            [
                Sequence[Mapping[str, Any]],
                Sequence[Mapping[str, Any]],
                Mapping[str, Any],
            ],
            dict[str, Any],
        ] = schedule,
    ) -> None:
        self.middleware = middleware
        self.resource_monitor = resource_monitor
        self.scheduler = scheduler
        self.worker = worker
        self.dispatcher_id = str(dispatcher_id).strip()
        if not self.dispatcher_id:
            raise DispatchError("dispatcher_id is required")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 1
        ):
            raise DispatchError("lease_seconds must be a positive integer")
        self.lease_seconds = lease_seconds
        self.last_auto_released: list[dict[str, Any]] = []
        self.last_auto_requeued: list[dict[str, Any]] = []
        self.last_triage: list[dict[str, Any]] = []

    def recover_once(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        """Observe one reconciling session before applying wall-proof recovery."""

        self.last_auto_released = []
        self.last_auto_requeued = []
        self.last_triage = []
        has_expired = getattr(self.middleware, "has_expired_leases", None)
        if not callable(has_expired) or has_expired(now=now):
            self.middleware.expire_leases(now=now)
        termination_result: dict[str, Any] | None = None
        has_terminations = getattr(self.middleware, "has_pending_terminations", None)
        if callable(has_terminations) and has_terminations():
            get_termination = getattr(self.middleware, "get_next_pending_termination", None)
            pending = get_termination() if callable(get_termination) else None
            if pending is not None:
                attempt_id = pending["attempt_id"]
                session_ref = pending["session_ref"]
                fn = getattr(self.worker, "terminate_session", None)
                if not callable(fn):
                    self.middleware.update_termination_state(attempt_id, "unavailable", now=now)
                    termination_result = {"attempt_id": attempt_id, "session_ref": session_ref, "termination": "unavailable"}
                else:
                    try:
                        raw_outcome = fn(session_ref)
                    except Exception as exc:
                        termination_result = {"attempt_id": attempt_id, "session_ref": session_ref, "termination": "requested", "error": {"kind": "adapter_exception", "type": type(exc).__name__, "message": str(exc)}}
                    else:
                        try:
                            outcome = normalize_session_termination(raw_outcome)
                        except ContractError as exc:
                            termination_result = {"attempt_id": attempt_id, "session_ref": session_ref, "termination": "requested", "error": {"kind": "invalid_adapter_outcome", "type": type(exc).__name__, "message": str(exc), "raw_outcome": repr(raw_outcome)}}
                        else:
                            if outcome in {"terminated", "absent"}:
                                self.middleware.update_termination_state(attempt_id, "confirmed", now=now)
                                termination_result = {"attempt_id": attempt_id, "session_ref": session_ref, "termination": "confirmed", "outcome": outcome}
                            else:
                                termination_result = {"attempt_id": attempt_id, "session_ref": session_ref, "termination": "requested", "outcome": outcome}

        has_reconciliation = getattr(self.middleware, "has_reconciliation_candidate", None)
        if callable(has_reconciliation) and not has_reconciliation():
            polled = None
        else:
            attempt = self.middleware.lease_next_reconciliation(self.dispatcher_id, self.lease_seconds, now=now)
            polled = None if attempt is None else self.poll_once(attempt["attempt_id"], now=now)

        # Observe first: an observed running session must not be wall-proofed
        # as lost in this cycle.
        auto_release = getattr(self.middleware, "auto_release_wall_budget", None)
        if callable(auto_release):
            has_wall = getattr(self.middleware, "has_reconciling_attempts_for_wall_proof", None)
            if not callable(has_wall) or has_wall():
                result = auto_release(now=now)
                if isinstance(result, list):
                    self.last_auto_released = result
        auto_requeue = getattr(self.middleware, "auto_requeue_recovering", None)
        if callable(auto_requeue):
            has_recovering = getattr(self.middleware, "has_recovering_evaluations", None)
            if not callable(has_recovering) or has_recovering():
                result = auto_requeue(now=now)
                if isinstance(result, list):
                    self.last_triage = result
                    self.last_auto_requeued = [item for item in result if item.get("action", "requeued") == "requeued"]
        if polled is None:
            return termination_result
        if termination_result is not None:
            return {**polled, "termination_action": termination_result}
        return polled

    def poll_once(
        self, attempt_id: str, *, now: datetime | None = None
    ) -> dict[str, Any]:
        attempt = self.middleware.get_attempt(attempt_id)
        if attempt["status"] in {"completed", "failed", "lost"}:
            # Re-entry of the termination path: best-effort, idempotent write of
            # the task-class feedback observation so statistics are fed exactly
            # once per terminal Attempt.
            record = getattr(self.middleware, "record_attempt_auto_feedback", None)
            if callable(record):
                record(attempt_id)
            return attempt
        if attempt["execution_plan"] is None or attempt["session_ref"] is None:
            raise DispatchError("Attempt is not bound to a simulation session")

        try:
            if attempt.get("allocation") is not None:
                self.worker.resume_session(
                    attempt["execution_plan"],
                    attempt["allocation"],
                    attempt["session_ref"],
                )
            observation = normalize_session_observation(
                self.worker.observe_session(attempt["session_ref"])
            )
        except ReceiptIntegrityError as exc:
            return self.middleware.fail_attempt(
                attempt_id,
                self.dispatcher_id,
                exc.failure_class,
                now=now,
            )
        except Exception:
            if attempt["status"] in {"running", "collecting"}:
                self.middleware.require_reconciliation(
                    attempt_id,
                    self.dispatcher_id,
                    reason="worker-observation-indeterminate",
                    now=now,
                )
            raise

        if observation == "running":
            if attempt["status"] == "reconciling":
                return self.middleware.reconcile_attempt(
                    attempt_id,
                    self.dispatcher_id,
                    attempt["session_ref"],
                    "running",
                    self.lease_seconds,
                    now=now,
                )
            if attempt["status"] != "running":
                raise DispatchError("running session does not match Attempt state")
            return self.middleware.heartbeat(
                attempt_id, self.dispatcher_id, self.lease_seconds, now=now
            )

        if observation in {"absent", "unreachable", "indeterminate"}:
            if attempt["status"] != "reconciling":
                attempt = self.middleware.require_reconciliation(
                    attempt_id,
                    self.dispatcher_id,
                    reason=f"worker-session-{observation}",
                    now=now,
                )
            return self.middleware.reconcile_attempt(
                attempt_id,
                self.dispatcher_id,
                attempt["session_ref"],
                observation,
                self.lease_seconds,
                now=now,
            )

        if attempt["status"] == "reconciling":
            attempt = self.middleware.reconcile_attempt(
                attempt_id,
                self.dispatcher_id,
                attempt["session_ref"],
                "completed",
                self.lease_seconds,
                now=now,
            )
        elif attempt["status"] == "running":
            attempt = self.middleware.begin_collection(
                attempt_id, self.dispatcher_id, now=now
            )
        elif attempt["status"] != "collecting":
            raise DispatchError("completed session does not match Attempt state")

        try:
            result, result_artifact_id = self.worker.collect_session(
                attempt["session_ref"]
            )
            return self.middleware.complete_session(
                result,
                self.dispatcher_id,
                result_artifact_id,
                now=now,
            )
        except ReceiptIntegrityError as exc:
            return self.middleware.fail_attempt(
                attempt_id,
                self.dispatcher_id,
                exc.failure_class,
                now=now,
            )
        except Exception:
            self.middleware.require_reconciliation(
                attempt_id,
                self.dispatcher_id,
                reason="worker-collection-indeterminate",
                now=now,
            )
            raise
