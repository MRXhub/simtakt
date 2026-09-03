"""Coordinate queue state, pure scheduling, and Worker lifecycle."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
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
def _orphan_timestamp(dt: datetime | None) -> str | None:
    """Encode an aware datetime as the repository's ISO timestamp form."""
    if dt is None or dt.tzinfo is None:
        return None
    return dt.isoformat()


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an orphan metadata ISO timestamp into an aware datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None
    return None


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
        if now is not None and getattr(now, "tzinfo", None) is None:
            raise ValueError("recover_once requires a timezone-aware now")

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
        # Orphan session loop: bounded observe + kill_at / TTL recovery of open
        # orphans runs after wall-proof release.  A failing orphan is isolated
        # and never aborts the recovery round.
        orphan_loop = getattr(self, "_reconcile_open_orphans", None)
        if callable(orphan_loop):
            orphan_loop(now=now)
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

    # ---- orphan session loop -----------------------------------------------
    def _orphan_batch_size(self) -> int:
        """Bound open-orphan processing per recovery round from the policy."""
        policy = getattr(self, "scheduling_policy", None)
        value = (
            getattr(policy, "orphan_batch_size", None) if policy is not None else None
        )
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return 10

    def _orphan_ttl_seconds(self) -> int:
        """Return the orphan Time-To-Live, falling back to a safe default."""
        policy = getattr(self, "scheduling_policy", None)
        value = (
            getattr(policy, "orphan_ttl_seconds", None) if policy is not None else None
        )
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return 604800

    def _record_orphan_event(
        self,
        orphan_id: str,
        from_status: str | None,
        to_status: str,
        event_type: str,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> None:
        """Best-effort orphan state-event write; never aborts recovery."""
        recorder = getattr(self.middleware, "record_orphan_state_event", None)
        if not callable(recorder):
            return
        try:
            recorder(
                orphan_id,
                from_status=from_status,
                to_status=to_status,
                event_type=event_type,
                payload=payload,
                now=now,
            )
        except Exception:
            return

    def _record_orphan_observe_failure(
        self, orphan: Mapping[str, Any], exc: Exception, now: datetime
    ) -> None:
        orphan_id = str(orphan.get("orphan_id") or "")
        if not orphan_id:
            return
        try:
            self._record_orphan_event(
                orphan_id,
                "open",
                "open",
                "OrphanObserveFailed",
                {
                    "orphan_id": orphan_id,
                    "attempt_id": orphan.get("attempt_id"),
                    "session_ref": orphan.get("session_ref"),
                    "error": {
                        "kind": "orphan-observe",
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                },
                now,
            )
        except Exception:
            return

    def _reconcile_open_orphans(self, *, now: datetime | None = None) -> int:
        """Observe and recover at most ``orphan_batch_size`` open orphans."""
        current = now or datetime.now().astimezone()
        list_open = getattr(self.middleware, "list_orphan_sessions", None)
        if not callable(list_open):
            return 0
        try:
            open_orphans = [
                item
                for item in list_open("open")
                if isinstance(item, dict) and item.get("status") == "open"
            ]
        except Exception:
            return 0
        processed = 0
        for orphan in open_orphans[: self._orphan_batch_size()]:
            try:
                if self._reconcile_one_orphan(orphan, current):
                    processed += 1
            except Exception as exc:
                # A failing orphan is isolated: record the failure as an orphan
                # state event and continue the bounded round with the next one.
                self._record_orphan_observe_failure(orphan, exc, current)
        return processed

    def _reconcile_one_orphan(self, orphan: Mapping[str, Any], now: datetime) -> bool:
        get_orphan = getattr(self.middleware, "get_orphan_session", None)
        update = getattr(self.middleware, "update_orphan_session", None)
        if not callable(get_orphan) or not callable(update):
            return False
        orphan_id = str(orphan["orphan_id"])
        try:
            latest = get_orphan(orphan_id)
        except Exception:
            return False
        if not isinstance(latest, dict) or latest.get("status") != "open":
            return False
        meta = dict(latest.get("metadata") or {})
        since_dt = _parse_timestamp(meta.get("orphan_since") or latest.get("created_at"))
        kill_dt = _parse_timestamp(meta.get("kill_at"))
        ttl_passed = since_dt is not None and now >= since_dt + timedelta(
            seconds=self._orphan_ttl_seconds()
        )

        kill_at_elapsed = kill_dt is not None and now >= kill_dt
        observation = self._observe_orphan(latest)
        # A finished session is harvested first, regardless of TTL expiry.
        if observation == "completed":
            return self._collect_orphan(latest, meta, now)
        if observation == "absent":
            meta["last_observed_status"] = "absent"
            meta["closed_at"] = _orphan_timestamp(now)
            update(orphan_id, status="closed", metadata=meta, now=now)
            return True
        if observation in {"running", "unreachable", "indeterminate"}:
            if kill_at_elapsed or ttl_passed:
                # Over-budget live session: terminate it.  The orphan stays open
                # (still holding its license) until the termination is confirmed
                # by a later round.
                return self._terminate_orphan(latest, meta, now)
            meta["last_observed_status"] = observation
            meta["last_observed_at"] = _orphan_timestamp(now)
            update(orphan_id, status="open", metadata=meta, now=now)
            return True
        # unobservable -> leave the orphan open for a later round.
        return False

    def _observe_orphan(self, orphan: Mapping[str, Any]) -> str | None:
        get_attempt = getattr(self.middleware, "get_attempt", None)
        if not callable(get_attempt):
            return None
        try:
            attempt = get_attempt(str(orphan.get("attempt_id") or ""))
        except Exception:
            return None
        if not isinstance(attempt, dict):
            return None
        plan = attempt.get("execution_plan")
        allocation = attempt.get("allocation")
        session_ref = orphan.get("session_ref")
        if plan is None or allocation is None or not session_ref:
            return None
        resume = getattr(self.worker, "resume_session", None)
        observe = getattr(self.worker, "observe_session", None)
        if not callable(resume) or not callable(observe):
            return None
        resume(plan, allocation, session_ref)
        return normalize_session_observation(observe(session_ref))

    def _collect_orphan(
        self, orphan: Mapping[str, Any], meta: dict[str, Any], now: datetime
    ) -> bool:
        """Collect a finished orphan session and harvest its lost Attempt."""
        orphan_id = str(orphan["orphan_id"])
        session_ref = orphan.get("session_ref")
        collect = getattr(self.worker, "collect_session", None)
        harvest = getattr(self.middleware, "harvest_orphan_session", None)
        update = getattr(self.middleware, "update_orphan_session", None)
        if not session_ref or not callable(collect) or not callable(harvest):
            # No collection capability: keep the orphan open for a later round.
            if callable(update) and session_ref:
                meta["last_observed_status"] = "completed"
                meta["last_observed_at"] = _orphan_timestamp(now)
                update(orphan_id, status="open", metadata=meta, now=now)
            return True
        try:
            result, artifact_id = collect(session_ref)
            harvest(result, self.dispatcher_id, artifact_id, session_ref, now=now)
        except Exception:
            # The orphan may already be closed by a committed harvest; never
            # abort the bounded recovery round.
            return True
        return True

    def _terminate_orphan(
        self, latest: Mapping[str, Any], meta: dict[str, Any], now: datetime
    ) -> bool:
        orphan_id = str(latest["orphan_id"])
        session_ref = latest.get("session_ref")
        terminate = getattr(self.worker, "terminate_session", None)
        meta["terminate_attempts"] = int(meta.get("terminate_attempts", 0) or 0) + 1
        meta["last_terminate_at"] = _orphan_timestamp(now)
        update = getattr(self.middleware, "update_orphan_session", None)
        if not callable(update):
            return True
        if not callable(terminate):
            # Termination is unavailable (no worker capability), so the
            # over-budget orphan cannot be killed; close it as expired and
            # record an orphan state event.
            meta["terminate_status"] = "unavailable"
            meta["closed_at"] = _orphan_timestamp(now)
            self._record_orphan_event(
                orphan_id,
                "open",
                "closed",
                "OrphanTerminationUnavailable",
                {
                    "orphan_id": orphan_id,
                    "attempt_id": latest.get("attempt_id"),
                    "session_ref": session_ref,
                    "reason": "terminate-unavailable",
                    "closed_at": meta["closed_at"],
                },
                now,
            )
            update(orphan_id, status="closed", metadata=meta, now=now)
            return True
        try:
            outcome = normalize_session_termination(terminate(session_ref))
        except Exception:
            meta["terminate_status"] = "requested"
            update(orphan_id, status="open", metadata=meta, now=now)
            return True
        if outcome in {"terminated", "absent"}:
            meta["terminate_status"] = "confirmed"
            meta["closed_at"] = _orphan_timestamp(now)
            update(orphan_id, status="closed", metadata=meta, now=now)
        else:
            meta["terminate_status"] = "requested"
            update(orphan_id, status="open", metadata=meta, now=now)
        return True

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
