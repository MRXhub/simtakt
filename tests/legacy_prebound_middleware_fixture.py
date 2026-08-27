"""Test-only fixture for exercising retired prebound Attempt behavior."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Mapping, Sequence

from control_plane.data.sqlite_evaluation_repository import (
    RepositoryError,
    SQLiteEvaluationRepository,
)
from control_plane.evaluation.service import EvaluationMiddleware
from control_plane.simulation.session_contracts import (
    make_simulation_session_plan,
    validate_simulation_session_plan,
)


class LegacyEvaluationMiddleware(EvaluationMiddleware):
    """Explicit superset containing the retired prebound-session operations."""

    @classmethod
    def from_sqlite(cls, path: str) -> "LegacyEvaluationMiddleware":
        return cls(SQLiteEvaluationRepository(path))

    @property
    def repository(self):
        """Expose the old repository handle only to compatibility consumers."""

        return self._repository

    def schedule_attempt(
        self,
        evaluation_id: str,
        *,
        simulation_adapter: str,
        numerical_profile: str,
        checkpoint_parent_attempt_id: str | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        return self._repository.schedule_attempt(
            evaluation_id=evaluation_id,
            simulation_adapter=simulation_adapter,
            numerical_profile=numerical_profile,
            checkpoint_parent_attempt_id=checkpoint_parent_attempt_id,
            attempt_id=attempt_id,
        )

    def cancel_planned_attempt(
        self, attempt_id: str, reason: str
    ) -> dict[str, Any]:
        return self._repository.cancel_planned_attempt(attempt_id, reason)

    def plan_prepared_session(
        self,
        evaluation_id: str,
        *,
        simulation_adapter: str,
        numerical_profile: str,
        recovery_profile_revision: str,
        base_package_artifact_id: str,
        base_package_revision: str,
        task_id: str,
        target_id: str,
        authorization_id: str,
        authorization_revision: str,
        requested_processors: int,
        command_timeout_seconds: int,
        max_solver_runs: int,
        max_wall_seconds: int,
        checkpoint_parent_attempt_id: str | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        evaluation = self._repository.get_evaluation(evaluation_id)
        requested_attempt_id = attempt_id or f"attempt:{uuid.uuid4()}"
        plan_inputs = {
            "evaluation_id": evaluation_id,
            "candidate_id": evaluation["candidate_id"],
            "simulation_proxy": simulation_adapter,
            "recovery_profile_revision": recovery_profile_revision,
            "base_package_artifact_id": base_package_artifact_id,
            "base_package_revision": base_package_revision,
            "task_id": task_id,
            "target_id": target_id,
            "authorization_id": authorization_id,
            "authorization_revision": authorization_revision,
            "requested_processors": requested_processors,
            "command_timeout_seconds": command_timeout_seconds,
            "max_solver_runs": max_solver_runs,
            "max_wall_seconds": max_wall_seconds,
        }
        plan = make_simulation_session_plan(
            attempt_id=requested_attempt_id,
            **plan_inputs,
        )
        attempt = self.schedule_attempt(
            evaluation_id,
            simulation_adapter=simulation_adapter,
            numerical_profile=numerical_profile,
            checkpoint_parent_attempt_id=checkpoint_parent_attempt_id,
            attempt_id=requested_attempt_id,
        )
        if attempt["attempt_id"] != requested_attempt_id:
            plan = make_simulation_session_plan(
                attempt_id=attempt["attempt_id"],
                **plan_inputs,
            )
        return self.bind_session_plan(plan)

    def lease_next(
        self,
        worker_id: str,
        lease_seconds: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        return self._repository.lease_next_attempt(
            worker_id, lease_seconds, now=now
        )

    def scheduling_candidates(self, limit: int = 32) -> list[dict[str, Any]]:
        return self._repository.list_scheduling_candidates(limit)

    def claim_scheduled_session(
        self,
        attempt_id: str,
        dispatcher_id: str,
        lease_seconds: int,
        allocation: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        return self._repository.claim_scheduled_session(
            attempt_id,
            dispatcher_id,
            lease_seconds,
            allocation,
            now=now,
        )

    def release_attempt(
        self,
        attempt_id: str,
        worker_id: str,
        *,
        reason: str = "capacity-wait",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self._repository.release_attempt(
            attempt_id, worker_id, reason=reason, now=now
        )

    def start_attempt(
        self, attempt_id: str, worker_id: str, *, now: datetime | None = None
    ) -> dict[str, Any]:
        return self._repository.start_attempt(attempt_id, worker_id, now=now)

    def start_session(
        self,
        plan: Mapping[str, Any],
        worker_id: str,
        session_ref: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized = self._validated_session_plan(plan)
        self._repository.bind_attempt_plan(
            normalized["attempt_id"], normalized["plan_id"], normalized
        )
        return self._repository.start_attempt(
            normalized["attempt_id"],
            worker_id,
            session_ref=session_ref,
            execution_plan_id=normalized["plan_id"],
            now=now,
        )

    def bind_session_plan(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        normalized = self._validated_session_plan(plan)
        return self._repository.bind_attempt_plan(
            normalized["attempt_id"], normalized["plan_id"], normalized
        )

    def _validated_session_plan(
        self, plan: Mapping[str, Any]
    ) -> dict[str, Any]:
        normalized = validate_simulation_session_plan(plan)
        attempt = self._repository.get_attempt(normalized["attempt_id"])
        evaluation = self._repository.get_evaluation(normalized["evaluation_id"])
        if (
            attempt["evaluation_id"] != normalized["evaluation_id"]
            or attempt["simulation_adapter"] != normalized["simulation_proxy"]
            or evaluation["candidate_id"] != normalized["candidate_id"]
        ):
            raise RepositoryError("session plan does not match the scheduled Attempt")
        return normalized

    def complete_attempt(
        self,
        attempt_id: str,
        worker_id: str,
        artifact_ids: Sequence[str],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Complete only an unprepared legacy Attempt during controlled drain."""

        attempt = self._repository.get_attempt(attempt_id)
        if attempt["execution_preparation_id"] is not None:
            raise RepositoryError(
                "legacy raw completion cannot complete a prepared Attempt"
            )
        return self._repository.complete_attempt(
            attempt_id, worker_id, artifact_ids, now=now
        )
