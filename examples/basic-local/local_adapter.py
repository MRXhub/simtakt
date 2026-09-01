#!/usr/bin/env python3
"""Pure local reference adapter for the control-plane simulation protocol.

This module is a self-contained *reference* implementation of the two runtime
boundaries that a governed simulation backend must satisfy:

* :class:`LocalGateway` implements ``control_plane.simulation.SimulationGateway``.
* :class:`LocalWorker` implements ``control_plane.simulation.SimulationWorker``.

Instead of talking to a remote TCAD cluster, each "session" is simulated as a
short local job: it sleeps briefly, performs a trivial computation, and writes a
result file into its durable session directory.  The adapter is deliberately
backend-independent and is intended for smoke tests, demos, and documentation
only -- it never requires any external solver or license.

No absolute paths are hard-coded here; every location is derived at runtime from
``sessions_root`` and the per-session directory handed to the gateway.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from control_plane.core.evaluation_contracts import canonical_json
from control_plane.simulation.session_contracts import (
    make_simulation_session_result,
    make_solver_run_record,
    normalize_session_ref,
)
from control_plane.simulation.worker import SimulationWorker

__all__ = [
    "LocalAdapterError",
    "LocalGateway",
    "LocalWorker",
]

# On-disk names used by the durable local worker.
PLAN_FILE = "session-plan.json"
ALLOCATION_FILE = "capacity-allocation.json"
LAUNCH_FILE = "launch-confirmed.json"
RESULT_FILE = "local-result.json"
RUN_FILE = "solver-run.json"
SESSION_RESULT_FILE = "simulation-result.json"


class LocalAdapterError(RuntimeError):
    """Raised when a local session cannot be started or collected."""


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalAdapterError(f"{label}: cannot read {path.name}") from exc
    if not isinstance(value, Mapping):
        raise LocalAdapterError(f"{label}: expected a JSON object")
    return dict(value)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(canonical_json(dict(value)) + "\n", encoding="utf-8")


class LocalGateway:
    """Implements the ``SimulationGateway`` protocol for a short local job.

    ``__call__`` runs the simulated job synchronously inside the caller's
    thread.  Because the work is tiny, the demo's worker dispatches it on a
    background thread so callers can observe the ``running`` -> ``completed``
    transition exactly like a real remote session.
    """

    launch_confirmation_kind = "local-short-job-confirmed"

    def __init__(
        self,
        *,
        artifact_root: Path | str | None = None,
        work_delay_seconds: float = 0.01,
    ) -> None:
        self.artifact_root = (
            None if artifact_root is None else Path(artifact_root).resolve()
        )
        self.work_delay_seconds = work_delay_seconds
        self._registered: dict[str, str] = {}

    # -- SimulationGateway surface -----------------------------------------

    def __call__(
        self,
        plan: Mapping[str, Any],
        allocation: Mapping[str, Any],
        session_ref: str,
        session_directory: Path,
    ) -> Mapping[str, Any]:
        directory = Path(session_directory)
        directory.mkdir(parents=True, exist_ok=True)

        # Simulated local short job: sleep + compute, then write the result.
        time.sleep(self.work_delay_seconds)
        processors = int(
            plan.get("resources", {})
            .get("requested_processors", allocation.get("processors", 1))
        )
        computed_value = processors * 2 + 1
        confirmation = {
            "schema_version": 1,
            "confirmation_kind": self.launch_confirmation_kind,
            "session_ref": str(session_ref),
            "run_id": str(allocation.get("run_id", "")),
            "target_id": str(plan.get("target_id", "local")),
            "processors": processors,
            "computed_value": computed_value,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json(directory / RESULT_FILE, confirmation)
        return confirmation

    def observe(self, confirmation: Mapping[str, Any]) -> str:
        # The job already finished while __call__ was executing.
        if str(confirmation.get("computed_value", "")).isdigit():
            return "completed"
        return "running"

    def recover_launch(
        self,
        plan: Mapping[str, Any],
        allocation: Mapping[str, Any],
        session_ref: str,
        session_directory: Path,
    ) -> Mapping[str, Any] | None:
        # Local jobs cannot survive a process restart; nothing to recover.
        return None

    def collect_runner_receipt(
        self,
        confirmation: Mapping[str, Any],
        plan: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return {
            "schema_version": 1,
            "receipt_kind": "local-run-receipt",
            "run_id": str(confirmation.get("run_id", "")),
            "exit_code": 0,
            "processors": int(confirmation.get("processors", 1)),
            "computed_value": int(confirmation.get("computed_value", 0)),
        }

    def publish_run_artifact(
        self,
        confirmation: Mapping[str, Any],
        plan: Mapping[str, Any],
        receipt: Mapping[str, Any],
    ) -> str:
        payload = {
            "schema_version": 1,
            "kind": "local-run-artifact",
            "run_id": str(confirmation.get("run_id", "")),
            "processors": int(receipt.get("processors", 1)),
            "computed_value": int(receipt.get("computed_value", 0)),
        }
        artifact_id = "artifact:" + hashlib.sha256(
            canonical_json(payload).encode("utf-8")
        ).hexdigest()
        if self.artifact_root is not None:
            self.artifact_root.mkdir(parents=True, exist_ok=True)
            path = self.artifact_root / (artifact_id + ".json")
            _write_json(path, payload)
            self.register_artifact(artifact_id, path, "local-run-artifact")
        return artifact_id

    def retry_action(
        self,
        plan: Mapping[str, Any],
        receipt: Mapping[str, Any],
        next_sequence: int,
    ) -> str | None:
        # Local jobs are deterministic; there is no retry policy to honor.
        return None

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
    ) -> Mapping[str, Any]:
        return self(
            plan, allocation, session_ref, session_directory
        )

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
    ) -> Mapping[str, Any] | None:
        return None

    def register_artifact(
        self, artifact_id: str, path: Path, kind: str
    ) -> None:
        self._registered[str(artifact_id)] = str(Path(path).name)

    def registered_artifacts(self) -> dict[str, str]:
        return dict(self._registered)


class LocalWorker(SimulationWorker):
    """Implements the ``SimulationWorker`` protocol with a :class:`LocalGateway`.

    Each started session owns a durable directory under ``sessions_root``.  The
    simulated job runs on a background thread so ``observe_session`` can report
    ``running`` before flipping to ``completed`` once the result file exists --
    mirroring the remote worker lifecycle without any external runtime.
    """

    def __init__(
        self,
        sessions_root: Path | str,
        gateway: LocalGateway | None = None,
    ) -> None:
        self.sessions_root = Path(sessions_root).resolve()
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        if gateway is None:
            gateway = LocalGateway(artifact_root=self.sessions_root / "artifacts")
        self.gateway = gateway
        self._threads: dict[str, threading.Thread] = {}
        self._results: dict[str, tuple[Mapping[str, Any], str]] = {}
        self._errors: dict[str, Exception] = {}

    # -- SimulationWorker surface ------------------------------------------

    def start_session(
        self,
        plan: Mapping[str, Any],
        allocation: Mapping[str, Any],
        session_ref: str,
    ) -> str:
        normalized = normalize_session_ref(session_ref)
        directory = self._directory(normalized)
        directory.mkdir(parents=True, exist_ok=True)
        _write_json(directory / PLAN_FILE, plan)
        _write_json(directory / ALLOCATION_FILE, allocation)

        thread = threading.Thread(
            target=self._run_job,
            args=(dict(plan), dict(allocation), normalized, directory),
            name=f"local-job-{normalized}",
            daemon=True,
        )
        self._threads[normalized] = thread
        thread.start()
        return "running"

    def resume_session(
        self,
        plan: Mapping[str, Any],
        allocation: Mapping[str, Any],
        session_ref: str,
    ) -> None:
        # Local sessions are ephemeral; resume only re-binds an in-flight run.
        normalized = normalize_session_ref(session_ref)
        directory = self._directory(normalized)
        if not directory.is_dir():
            raise LocalAdapterError(f"{normalized}: unknown local session")

    def observe_session(self, session_ref: str) -> str:
        normalized = normalize_session_ref(session_ref)
        directory = self._directory(normalized)
        if not (directory / RESULT_FILE).is_file():
            return "running"
        # Ensure the background thread finalized the session record.
        self._wait_finalized(normalized, directory)
        return "completed"

    def collect_session(
        self, session_ref: str
    ) -> tuple[Mapping[str, Any], str]:
        normalized = normalize_session_ref(session_ref)
        directory = self._directory(normalized)
        self._wait_finalized(normalized, directory)
        if not (directory / SESSION_RESULT_FILE).is_file():
            raise LocalAdapterError(f"{normalized}: session is not completed")
        result = _read_json(directory / SESSION_RESULT_FILE, "session result")
        return result, self._results[normalized][1]

    # -- internal helpers --------------------------------------------------

    def _directory(self, session_ref: str) -> Path:
        return self.sessions_root / f"session-{session_ref}"

    def _run_job(
        self,
        plan: Mapping[str, Any],
        allocation: Mapping[str, Any],
        session_ref: str,
        directory: Path,
    ) -> None:
        try:
            confirmation = self.gateway(
                plan, allocation, session_ref, directory
            )
            _write_json(directory / LAUNCH_FILE, confirmation)
            observation = self.gateway.observe(confirmation)
            if observation != "completed":
                raise LocalAdapterError(
                    f"{session_ref}: local job did not complete"
                )
            receipt = self.gateway.collect_runner_receipt(confirmation, plan)
            artifact_id = self.gateway.publish_run_artifact(
                confirmation, plan, receipt
            )
            run = make_solver_run_record(
                plan_id=plan["plan_id"],
                sequence=1,
                run_id=str(confirmation.get("run_id", "")),
                package_artifact_id=plan["base_package"]["artifact_id"],
                package_revision=plan["base_package"]["revision"],
                numerical_profile_revision=plan["recovery_profile_revision"],
                action="initial",
                status="completed",
                exit_code=0,
                artifact_ids=[artifact_id],
            )
            _write_json(directory / RUN_FILE, run)
            result = make_simulation_session_result(
                plan_id=plan["plan_id"],
                attempt_id=plan["attempt_id"],
                session_ref=session_ref,
                status="completed",
                solver_run_record_ids=[run["record_id"]],
                journal_artifact_id="artifact.local-session-journal",
                evidence_artifact_ids=[artifact_id],
            )
            _write_json(directory / SESSION_RESULT_FILE, result)
            self._results[session_ref] = (result, artifact_id)
        except Exception as exc:  # surface in observe/collect
            self._errors[session_ref] = exc
        finally:
            self._threads.pop(session_ref, None)

    def _wait_finalized(self, session_ref: str, directory: Path) -> None:
        thread = self._threads.get(session_ref)
        if thread is not None:
            thread.join(timeout=10)
        if session_ref in self._errors:
            error = self._errors.pop(session_ref)
            raise LocalAdapterError(f"{session_ref}: {error}") from error
        _ = directory
