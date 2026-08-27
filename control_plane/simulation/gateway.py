"""Runtime boundary implemented by one concrete simulation backend."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Protocol


class ReceiptIntegrityError(RuntimeError):
    """A terminal receipt mismatch that must not be treated as a live run."""

    failure_class = "runner-receipt-integrity-mismatch"


class SimulationGateway(Protocol):
    """The only surface a Worker may use to reach a simulation runtime."""

    launch_confirmation_kind: str

    def __call__(
        self,
        plan: Mapping[str, Any],
        allocation: Mapping[str, Any],
        session_ref: str,
        session_directory: Path,
    ) -> Mapping[str, Any]: ...

    def observe(self, confirmation: Mapping[str, Any]) -> str: ...

    def recover_launch(
        self,
        plan: Mapping[str, Any],
        allocation: Mapping[str, Any],
        session_ref: str,
        session_directory: Path,
    ) -> Mapping[str, Any] | None: ...

    def collect_runner_receipt(
        self, confirmation: Mapping[str, Any], plan: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def publish_run_artifact(
        self,
        confirmation: Mapping[str, Any],
        plan: Mapping[str, Any],
        receipt: Mapping[str, Any],
    ) -> str: ...

    def retry_action(
        self,
        plan: Mapping[str, Any],
        receipt: Mapping[str, Any],
        next_sequence: int,
    ) -> str | None: ...

    def start_retry(
        self,
        plan: Mapping[str, Any],
        allocation: Mapping[str, Any],
        session_ref: str,
        session_directory: Path,
        *,
        run_id: str,
        sequence: int,
        action: str,
    ) -> Mapping[str, Any]: ...

    def recover_retry(
        self,
        plan: Mapping[str, Any],
        allocation: Mapping[str, Any],
        session_ref: str,
        session_directory: Path,
        *,
        run_id: str,
        sequence: int,
        action: str,
    ) -> Mapping[str, Any] | None: ...

    def register_artifact(
        self, artifact_id: str, path: Path, kind: str
    ) -> None: ...
