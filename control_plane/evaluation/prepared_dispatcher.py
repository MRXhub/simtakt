"""Dispatch prepared choices without accepting an upstream SessionPlan."""

import logging
import uuid
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from datetime import datetime
from typing import Any, Protocol

from control_plane.evaluation.dispatcher import DispatchError, SessionLifecycleDispatcher
from control_plane.evaluation.execution_topology import (
    ExecutionTopologyError,
    ensure_formal_targets_ready,
)
from control_plane.evaluation.execution_options import (
    ExecutionOptionError,
    validate_execution_preparation,
)
from control_plane.evaluation.compute_profile import (
    ComputeProfileError,
    make_task_class,
    validate_task_override,
)
from control_plane.evaluation.execution_planning import materialize_session_plan
from control_plane.evaluation.governed_preparation import GovernedPreparationError
from control_plane.evaluation.scheduling import (
    make_resource_allocation,
    scheduling_decision_plain,
)
from control_plane.evaluation.scheduling_policy import GovernedSchedulingPolicy
from control_plane.simulation.worker import SESSION_START_OUTCOMES, SessionStartFailure

MAX_PREPARED_CANDIDATE_WINDOW = 64
_SESSION_START_OUTCOME_PATHS = {
    "not_started": "fail",
    "preflight_failed": "fail",
    "absent": "fail",
    "unreachable": "reconcile",
    "launch_confirmed": "confirm",
    "indeterminate": "reconcile",
}
if set(_SESSION_START_OUTCOME_PATHS) != set(SESSION_START_OUTCOMES):
    raise RuntimeError("SESSION_START_OUTCOMES lacks an explicit dispatcher path")
_LOG = logging.getLogger(__name__)



class ScheduleOverrideProvider(Protocol):
    def get_override(self, task_class: Mapping[str, Any]) -> Mapping[str, Any] | None: ...


class _EmptyScheduleOverrideProvider:
    def get_override(self, task_class: Mapping[str, Any]) -> None:
        return None


class PreparedExecutionDispatcher(SessionLifecycleDispatcher):
    """The new control path: Scheduler selects before SessionPlan creation."""

    def __init__(
        self,
        *args: Any,
        preparation_governance: Callable[
            [Mapping[str, Any], datetime | None], Mapping[str, Any]
        ],
        scheduling_policy: GovernedSchedulingPolicy,
        execution_topology: Mapping[str, Any] | None = None,
        schedule_override_provider: ScheduleOverrideProvider | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if not callable(preparation_governance):
            raise DispatchError("preparation_governance is required")
        if not isinstance(scheduling_policy, GovernedSchedulingPolicy):
            raise DispatchError(
                "scheduling_policy must come from the project resolver"
            )
        self.preparation_governance = preparation_governance
        self.scheduling_policy = scheduling_policy
        self.execution_topology = execution_topology
        self.schedule_override_provider = schedule_override_provider or _EmptyScheduleOverrideProvider()

    def dispatch_once(
        self, *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        current_time = now or datetime.now().astimezone()
        if current_time.tzinfo is None:
            raise DispatchError("dispatch time must include a timezone")
        initial = self.middleware.prepared_scheduling_candidates(limit=MAX_PREPARED_CANDIDATE_WINDOW)
        if not initial:
            return []
        _, initial_candidates = self._eligible_prepared_candidates(initial, now=current_time)
        if not initial_candidates:
            return []
        target_ids = self._formal_target_ids(initial_candidates)
        if len(target_ids) > 1:
            ensure_formal_targets_ready(self.execution_topology or {
                "targets": [
                    {"target_id": target_id, "formal_execution": True,
                     "status": "active", "host_id": None}
                    for target_id in target_ids
                ],
                "formal_target_ids": target_ids,
            })
        global_lock = getattr(self.resource_monitor, "locked_dispatch", None)
        if len(target_ids) > 1 and not callable(global_lock):
            raise DispatchError(
                "multi-target dispatch requires resource_monitor.locked_dispatch"
            )
        # Single-target dispatch keeps the compatibility path for older test
        # and dry-run monitors that only expose locked_snapshot.  The target
        # lock still covers observation through reliable Worker start.
        dispatch_lock = global_lock() if len(target_ids) > 1 else nullcontext()
        with dispatch_lock:
            launched: list[dict[str, Any]] = []
            for target_id in target_ids:
                outcome = self._dispatch_single_target(target_id, now=current_time)
                if outcome is None:
                    continue
                if isinstance(outcome, Mapping) and outcome.get("action") == "wait":
                    continue
                launched.append(outcome)
            return launched

    def _formal_target_ids(self, candidates: list[dict[str, Any]]) -> list[str]:
        if self.execution_topology is not None:
            return sorted({str(value) for value in self.execution_topology.get("formal_target_ids", ())})
        return sorted({
            option["target_id"]
            for candidate in candidates
            for option in candidate["execution_option_set"]["options"]
        })

    def _capacity_scope(self, target_id: str) -> set[str]:
        if self.execution_topology is None:
            return {target_id}
        records = self.execution_topology.get("targets", ())
        target_record = next(
            (item for item in records
             if isinstance(item, Mapping) and item.get("target_id") == target_id),
            None,
        )
        host_id = target_record.get("host_id") if isinstance(target_record, Mapping) else None
        if not host_id:
            return {target_id}
        formal_ids = set(self._formal_target_ids([]))
        return {
            str(item["target_id"])
            for item in records
            if isinstance(item, Mapping)
            and item.get("target_id") in formal_ids
            and item.get("host_id") == host_id
        }

    def _dispatch_single_target(
        self, target: str | None = None, *, now: datetime | None = None
    ) -> dict[str, Any] | None:
        current_time = now or datetime.now().astimezone()
        if current_time.tzinfo is None:
            raise DispatchError("dispatch time must include a timezone")
        initial = self.middleware.prepared_scheduling_candidates(limit=MAX_PREPARED_CANDIDATE_WINDOW)
        if not initial:
            return None
        initial, initial_candidates = self._eligible_prepared_candidates(
            initial, now=current_time
        )
        if not initial_candidates:
            return None
        target_ids = {
            option["target_id"]
            for candidate in initial_candidates
            for option in candidate["execution_option_set"]["options"]
        }
        if target is None:
            if len(target_ids) != 1:
                raise DispatchError(
                    "one scheduling window must resolve to exactly one governed target"
                )
            target = next(iter(target_ids))
        elif target not in target_ids:
            return None
        with self.resource_monitor.locked_snapshot(target) as resources:
            current = self.middleware.prepared_scheduling_candidates(limit=MAX_PREPARED_CANDIDATE_WINDOW)
            if not current:
                return None
            current, candidates = self._eligible_prepared_candidates(
                current, now=current_time
            )
            if not candidates:
                return None
            if not any(
                option["target_id"] == target
                for candidate in candidates
                for option in candidate["execution_option_set"]["options"]
            ):
                return None
            profile_identities = self._profile_identities(candidates)
            capacity_snapshot = self.middleware.capacity_profile_snapshot(profile_identities)
            overrides = self._current_overrides(candidates)
            active_allocations = self.middleware.active_allocations()
            scheduling_policy = {
                "priority_order": list(self.scheduling_policy.priority_order),
                "default_priority": self.scheduling_policy.default_priority,
                "aging_quantum_seconds": (
                    self.scheduling_policy.aging_quantum_seconds
                ),
            }
            governed_capacity = self.scheduling_policy.as_mapping()[
                "capacity_envelope"
            ]
            capacity_envelope = {
                key: governed_capacity[key]
                for key in ("processors", "memory_bytes", "license_sessions")
            }
            if "license_reserve" in governed_capacity:
                capacity_envelope["license_reserve"] = governed_capacity[
                    "license_reserve"
                ]
            decision = self.scheduler(
                candidates,
                active_allocations,
                resources,
                option_policy=self.scheduling_policy.option_policy,
                scheduling_policy=scheduling_policy,
                decision_time=current_time,
                capacity_envelope=capacity_envelope,
                capacity_profile_snapshot=capacity_snapshot,
                overrides=overrides,
                scheduling_policy_provenance=self.scheduling_policy.provenance(),
                capacity_scope=self._capacity_scope(target),
            )
            try:
                artifact_id, artifact_path = self.resource_monitor.record_decision(
                    decision,
                    candidates,
                    active_allocations,
                    resources,
                    scheduling_policy=scheduling_policy,
                    decision_time=current_time,
                    capacity_envelope=capacity_envelope,
                    capacity_profile_snapshot=capacity_snapshot,
                    capacity_scope=self._capacity_scope(target),
                    task_classes=[candidate["task_class"] for candidate in candidates],
                    overrides=overrides,
                    scheduling_policy_provenance=self.scheduling_policy.provenance(),
                )
            except TypeError as exc:
                # Keep older test/dry-run monitors source-compatible; the
                # governed RemoteResourceMonitor accepts and persists all
                # adaptive evidence above.
                if "unexpected keyword" not in str(exc):
                    raise
                artifact_id, artifact_path = self.resource_monitor.record_decision(
                    decision, candidates, active_allocations, resources,
                    scheduling_policy=scheduling_policy,
                    decision_time=current_time,
                    capacity_envelope=capacity_envelope,
                )
            if decision["action"] == "wait":
                return {
                    "action": "wait",
                    "decision": decision,
                    "scheduling_decision_artifact_id": artifact_id,
                    "scheduling_decision_path": str(artifact_path),
                }

            selected_id = decision["selected_attempt_id"]
            selected = next(
                (item for item in current if item["attempt_id"] == selected_id),
                None,
            )
            if selected is None:
                raise DispatchError("scheduler selected an unknown Attempt")
            selected_option = decision.get("selected_execution_option")
            if not isinstance(selected_option, Mapping):
                raise DispatchError(
                    "prepared scheduling decision lacks one execution option"
                )
            # Scheduler results are deeply immutable; downstream validators and
            # persistence consume the supported plain form.
            selected_option = scheduling_decision_plain(selected_option)
            preparation = self._governed_or_retire(
                selected, now=current_time
            )
            if preparation is None:
                return self.middleware.get_attempt(selected_id)
            plan = materialize_session_plan(
                attempt_id=selected_id,
                preparation=preparation,
                selected_option=selected_option,
            )
            wall_clock = datetime.now().astimezone()
            run_id = wall_clock.strftime("%Y%m%d-%H%M%S-") + (
                f"{wall_clock.microsecond // 1000:03d}"
            )
            session_ref = "session-" + uuid.uuid4().hex
            allocation = make_resource_allocation(
                decision,
                session_ref=session_ref,
                run_id=run_id,
                remote_workspace_root=resources["remote_workspace_root"],
                decision_artifact_id=artifact_id,
                decision_artifact_path=str(artifact_path),
            )
            preparation = self._governed_or_retire(
                selected, now=current_time
            )
            if preparation is None:
                return self.middleware.get_attempt(selected_id)
            attempt = self.middleware.claim_prepared_execution(
                selected_id,
                self.dispatcher_id,
                self.lease_seconds,
                preparation_id=preparation["preparation_id"],
                selected_option_id=selected_option["option_id"],
                session_plan=plan,
                allocation=allocation,
                license_sessions=(
                    capacity_envelope["license_sessions"]
                    - capacity_envelope.get("license_reserve", 0)
                ),
                now=current_time,
            )
            if attempt is None:
                raise DispatchError("selected Attempt changed before atomic claim")
            stored_plan = attempt["execution_plan"]
            stored_allocation = attempt["allocation"]
            stored_session_ref = attempt["session_ref"]
            if (
                not isinstance(stored_plan, Mapping)
                or not isinstance(stored_allocation, Mapping)
                or not isinstance(stored_session_ref, str)
            ):
                raise DispatchError("atomic claim did not persist execution facts")
            try:
                self.worker.start_session(
                    stored_plan, stored_allocation, stored_session_ref
                )
            except SessionStartFailure as exc:
                path = _SESSION_START_OUTCOME_PATHS.get(exc.outcome)
                if path == "reconcile":
                    self.middleware.require_reconciliation(
                        selected_id,
                        self.dispatcher_id,
                        reason="worker-start-" + exc.outcome,
                        now=current_time,
                    )
                elif path == "fail":
                    self.middleware.fail_attempt(
                        selected_id,
                        self.dispatcher_id,
                        exc.failure_class,
                        now=current_time,
                    )
                else:
                    raise DispatchError(
                        f"unhandled SessionStartFailure outcome: {exc.outcome}"
                    ) from exc
                return self.middleware.get_attempt(selected_id)
            except Exception:
                self.middleware.require_reconciliation(
                    selected_id,
                    self.dispatcher_id,
                    reason="worker-start-indeterminate",
                    now=current_time,
                )
                raise
            return self.middleware.confirm_attempt_start(
                selected_id, self.dispatcher_id, now=current_time
            )
    def _governed_preparation(
        self,
        value: Mapping[str, Any],
        *,
        now: datetime | None,
    ) -> dict[str, Any]:
        preparation = validate_execution_preparation(value)
        governed = validate_execution_preparation(
            self.preparation_governance(preparation, now)
        )
        if governed != preparation:
            raise DispatchError(
                "queued execution preparation is not currently governed"
            )
        return governed

    def _prepared_candidate(
        self,
        attempt: Mapping[str, Any],
        *,
        now: datetime | None,
    ) -> dict[str, Any]:
        preparation = self._governed_preparation(
            attempt["execution_preparation"], now=now
        )
        option_set = preparation["execution_option_set"]
        definition = option_set["options"][0]["simulation_definition"]
        task_class = make_task_class(
            simulation_definition_artifact_id=definition["artifact_id"],
            simulation_definition_revision=definition["revision"],
            numerical_profile=preparation["numerical_profile"],
            recovery_profile_revision=preparation["recovery_profile_revision"],
        )
        candidate = {
            "attempt_id": attempt["attempt_id"],
            "evaluation_id": attempt["evaluation_id"],
            "priority": attempt["priority"],
            "queued_since": attempt["queued_since"],
            "task_class": task_class,
            "execution_option_set": option_set,
            "performance_profile_snapshot": preparation[
                "performance_profile_snapshot"
            ],
        }
        if "calibration" in preparation:
            candidate["calibration"] = preparation["calibration"]
        return candidate

    def _eligible_prepared_candidates(
        self,
        attempts: list[dict[str, Any]],
        *,
        now: datetime,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        eligible_attempts: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        for attempt in attempts:
            try:
                candidate = self._prepared_candidate(attempt, now=now)
            except (ExecutionOptionError, ComputeProfileError, GovernedPreparationError, DispatchError) as exc:
                self._retire_rejected_preparation(attempt, now=now, error=exc)
                continue
            eligible_attempts.append(attempt)
            candidates.append(candidate)
        return eligible_attempts, candidates

    @staticmethod
    def _profile_identities(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        identities: dict[tuple[str, str], dict[str, Any]] = {}
        for candidate in candidates:
            boundary = candidate["task_class"]["boundary"]
            definition = boundary["simulation_definition"]
            for option in candidate["execution_option_set"]["options"]:
                identity = {
                    "simulation_definition_artifact_id": definition["artifact_id"],
                    "simulation_definition_revision": definition["revision"],
                    "numerical_profile": boundary["numerical_profile"],
                    "recovery_profile_revision": boundary["recovery_profile_revision"],
                    "target_id": option["target_id"],
                }
                if "user_class_key" in boundary:
                    identity["user_class_key"] = boundary["user_class_key"]
                identities[(candidate["task_class"]["key"], option["target_id"])] = identity
        return [identities[key] for key in sorted(identities)]

    def _current_overrides(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        values: dict[str, dict[str, Any]] = {}
        classes = {candidate["task_class"]["key"]: candidate["task_class"] for candidate in candidates}
        for key in sorted(classes):
            supplied = self.schedule_override_provider.get_override(classes[key])
            if supplied is None:
                continue
            raw = supplied.get("override") if isinstance(supplied, Mapping) and "override" in supplied else supplied
            try:
                override = validate_task_override(raw)
            except (ComputeProfileError, TypeError) as exc:
                raise DispatchError("schedule override provider returned an invalid override") from exc
            if override["task_class_key"] != key:
                raise DispatchError("schedule override does not match its task class")
            if key in values and values[key] != override:
                raise DispatchError("schedule override provider returned ambiguous class")
            values[key] = override
        return [values[key] for key in sorted(values)]

    def _governed_or_retire(
        self,
        attempt: Mapping[str, Any],
        *,
        now: datetime,
    ) -> dict[str, Any] | None:
        try:
            return self._governed_preparation(
                attempt["execution_preparation"], now=now
            )
        except (ExecutionOptionError, GovernedPreparationError, DispatchError) as exc:
            self._retire_rejected_preparation(attempt, now=now, error=exc)
            return None

    def _retire_rejected_preparation(
        self,
        attempt: Mapping[str, Any],
        *,
        now: datetime,
        error: Exception,
    ) -> None:
        preparation = attempt.get("execution_preparation")
        attempt_id = str(attempt.get("attempt_id", "")).strip()
        preparation_id = (
            str(preparation.get("preparation_id", "")).strip()
            if isinstance(preparation, Mapping)
            else ""
        )
        if not attempt_id or not preparation_id:
            raise DispatchError(
                "rejected Preparation lacks a retireable identity"
            ) from error
        reason = (
            "preparation-contract-invalid"
            if isinstance(error, (ExecutionOptionError, ComputeProfileError))
            else "preparation-governance-rejected"
        )
        _LOG.warning(
            "preparation skipped attempt_id=%s preparation_id=%s reason=%s error=%s",
            attempt_id,
            preparation_id,
            reason,
            error,
        )
        self.middleware.retire_unstarted_preparation(
            attempt_id,
            preparation_id,
            reason=reason,
            now=now,
        )
