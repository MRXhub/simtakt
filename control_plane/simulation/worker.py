"""Session-lifecycle boundary used by the evaluation dispatcher."""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from control_plane.core.evaluation_contracts import ContractError


SESSION_START_OUTCOMES = frozenset(
    {
        "not_started",
        "preflight_failed",
        "absent",
        "unreachable",
        "launch_confirmed",
        "indeterminate",
    }
)
SESSION_OBSERVATIONS = frozenset(
    {"running", "completed", "absent", "unreachable", "indeterminate"}
)
SESSION_TERMINATIONS = frozenset(
    {"terminated", "absent", "unreachable", "indeterminate"}
)


def normalize_session_termination(value: Any) -> str:
    termination = str(value).strip().lower()
    if termination not in SESSION_TERMINATIONS:
        raise ContractError(
            "session termination must be terminated, absent, unreachable, or indeterminate"
        )
    return termination




class SessionStartFailure(RuntimeError):
    """A classified failure before an exact Solver Run launch is confirmed."""

    def __init__(self, outcome: str, failure_class: str, message: str) -> None:
        normalized = str(outcome).strip().lower()
        if normalized not in SESSION_START_OUTCOMES - {"launch_confirmed"}:
            raise ValueError(f"invalid session start failure outcome: {outcome}")
        failure = str(failure_class).strip()
        if not failure:
            raise ValueError("session start failure_class is required")
        super().__init__(message)
        self.outcome = normalized
        self.failure_class = failure


def normalize_session_observation(value: Any) -> str:
    observation = str(value).strip().lower()
    if observation == "unknown":
        observation = "indeterminate"
    if observation not in SESSION_OBSERVATIONS:
        raise ContractError(
            "session observation must be running, completed, absent, "
            "unreachable, or indeterminate"
        )
    return observation


class SimulationWorker(Protocol):
    """Start, observe, and collect durable Sessions without queue access."""

    def start_session(
        self,
        plan: Mapping[str, Any],
        allocation: Mapping[str, Any],
        session_ref: str,
    ) -> None: ...

    def resume_session(
        self,
        plan: Mapping[str, Any],
        allocation: Mapping[str, Any],
        session_ref: str,
    ) -> None: ...

    def observe_session(self, session_ref: str) -> str: ...

    def collect_session(
        self, session_ref: str
    ) -> tuple[Mapping[str, Any], str]: ...

    # Optional adapter capability: dispatchers probe this with getattr; absence is valid.
    def terminate_session(self, session_ref: str) -> str: ...
