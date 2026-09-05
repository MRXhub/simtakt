"""Minimal fixed-quota monitor and fake worker for runtime examples.

Real deployments should replace the monitor with an adapter querying local
CPU/memory, the site's scheduler (such as Slurm), and the approved license
service. TODO(adapter): integrate authoritative site-specific APIs.
"""
from __future__ import annotations
import csv
import hashlib
import json
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from control_plane.core.evaluation_contracts import make_qualification_report
from control_plane.simulation.session_contracts import (
    make_simulation_session_result,
    make_solver_run_record,
)


class FixedQuotaResourceMonitor:
    def __init__(self, entry: Mapping[str, Any]):
        cfg = entry.get("config", {})
        self.root = Path(cfg.get("runtime_dir", "runtime")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.quota = {"processors": int(cfg.get("processors", 4)), "memory_bytes": int(cfg.get("memory_bytes", 1)), "license_sessions": int(cfg.get("license_sessions", 2))}
        self.lock_path = self.root / "resource.lock"
        self.receipts = self.root / "decisions"
        self.receipts.mkdir(parents=True, exist_ok=True)
        self.remote_workspace_root = str(
            cfg.get("remote_workspace_root", "/minimal-runtime")
        )

    def open(self) -> None: self.receipts.mkdir(parents=True, exist_ok=True)
    def close(self) -> None: pass

    @contextmanager
    def _lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 30.0
        owner = {"pid": os.getpid(), "created_at": time.time()}
        payload = json.dumps(owner, sort_keys=True).encode("utf-8")
        while True:
            try:
                fd = os.open(
                    self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                try:
                    os.write(fd, payload)
                finally:
                    os.close(fd)
                break
            except FileExistsError:
                stale = False
                try:
                    record = json.loads(self.lock_path.read_text(encoding="utf-8"))
                    pid = int(record.get("pid", -1))
                    created = float(record.get("created_at", 0))
                    stale = time.time() - created > 300
                    if not stale and pid > 0 and pid != os.getpid():
                        try:
                            if os.name == "nt":
                                # os.kill(pid, 0) sends a console signal on Windows.
                                result = subprocess.run(
                                    ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                                    capture_output=True, timeout=5, check=False,
                                )
                                rows = csv.reader(result.stdout.decode("mbcs", errors="replace").splitlines())
                                stale = result.returncode == 0 and not any(
                                    len(row) >= 2 and row[1].strip() == str(pid) for row in rows
                                )
                            else:
                                os.kill(pid, 0)
                        except ProcessLookupError:
                            stale = True
                        except (OSError, subprocess.TimeoutExpired):
                            # An unavailable probe is not evidence of a dead owner.
                            pass
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    stale = True
                if stale:
                    try:
                        self.lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"resource lock is held by a live owner: {self.lock_path}"
                    )
                time.sleep(0.005)
        try:
            yield
        finally:
            try:
                record = json.loads(self.lock_path.read_text(encoding="utf-8"))
                if int(record.get("pid", -1)) == os.getpid():
                    self.lock_path.unlink()
            except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
                pass

    def _make_snapshot(self, target_id: str) -> dict[str, Any]:
        quota = self.quota
        return {
            "schema_version": 1,
            "snapshot_kind": "resource-snapshot",
            "snapshot_revision": "sha256:" + hashlib.sha256(
                json.dumps(
                    {
                        "target_id": target_id,
                        "processors": quota["processors"],
                        "memory_bytes": quota["memory_bytes"],
                        "license_sessions": quota["license_sessions"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "target_id": target_id,
            "status": "ready",
            "available_processors": quota["processors"],
            "available_memory_bytes": quota["memory_bytes"],
            "default_request_memory_bytes": max(
                1, quota["memory_bytes"] // quota["processors"]
            ),
            "observed_allocation_keys": [],
            "reasons": [],
            "license_sessions_in_use": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "lock_held": True,
            "target_is_idle": True,
            "remote_workspace_root": self.remote_workspace_root,
        }

    @contextmanager
    def locked_snapshot(self, target_id: str):
        with self._lock():
            yield self._make_snapshot(target_id)

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
    def __init__(self, entry: Mapping[str, Any]):
        self.sessions: dict[str, str] = {}
        self.plans: dict[str, Mapping[str, Any]] = {}
        self.delay = float(entry.get("config", {}).get("delay_seconds", 0.001))
        self.adapter = MinimalSimulationAdapter({"config": {}})

    def _package(self, plan: Mapping[str, Any]) -> Mapping[str, Any]:
        base = plan.get("base_package")
        if isinstance(base, Mapping):
            return base
        materialized = self.adapter.materialize_package(
            {"candidate_id": plan.get("candidate_id")},
            {"candidate_id": plan.get("candidate_id")},
        )
        return materialized

    def start_session(self, plan: Mapping[str, Any], allocation: Mapping[str, Any], session_ref: str) -> None:
        if session_ref in self.sessions:
            return
        # The allocation's remote_workspace_root is a POSIX-logical token; the
        # fake worker materializes into its own configured .runtime package dir
        # so no drive-root or host path is ever created.
        template = plan.get("template", {"candidate_id": plan.get("candidate_id")})
        parameters = plan.get("candidate_parameters", {})
        self.adapter.materialize_package(
            template if isinstance(template, Mapping) else {},
            parameters if isinstance(parameters, Mapping) else {},
        )
        self.plans[session_ref] = dict(plan)
        self.sessions[session_ref] = "completed"
        if self.delay:
            time.sleep(self.delay)

    def resume_session(self, plan: Mapping[str, Any], allocation: Mapping[str, Any], session_ref: str) -> None:
        self.sessions.setdefault(session_ref, "running")

    def observe_session(self, session_ref: str) -> str:
        return self.sessions.get(session_ref, "absent")

    def collect_session(self, session_ref: str) -> tuple[Mapping[str, Any], str]:
        plan = self.plans.get(session_ref, {})
        package = self._package(plan)
        package_artifact_id = str(package.get("artifact_id") or "package.minimal.input")
        package_revision = str(package.get("revision") or "sha256:" + "0" * 64)
        plan_id = str(plan.get("plan_id") or "")
        attempt_id = str(plan.get("attempt_id") or "")
        recovery = str(
            plan.get("recovery_profile_revision")
            or package_revision
        )
        artifact_id = "minimal-runtime." + uuid.uuid4().hex
        if not plan_id or not attempt_id:
            # The dispatcher always binds a SessionPlan before collect; if it
            # does not, surface a clear contract error instead of a fake id.
            raise ValueError("collect_session requires a bound SessionPlan")
        run = make_solver_run_record(
            plan_id=plan_id,
            sequence=1,
            run_id=session_ref,
            package_artifact_id=package_artifact_id,
            package_revision=package_revision,
            numerical_profile_revision=recovery,
            action="initial",
            status="completed",
            exit_code=0,
            artifact_ids=[artifact_id],
        )
        result = make_simulation_session_result(
            plan_id=plan_id,
            attempt_id=attempt_id,
            session_ref=session_ref,
            status="completed",
            solver_run_record_ids=[run["record_id"]],
            journal_artifact_id=artifact_id,
            evidence_artifact_ids=[artifact_id],
        )
        self.sessions[session_ref] = "completed"
        return result, artifact_id

    def terminate_session(self, session_ref: str) -> str:
        self.sessions[session_ref] = "terminated"
        return "terminated"


class _MinimalGateway:
    """Small gateway used only to make the adapter boundary executable."""

    launch_confirmation_kind = "minimal-runtime-launch"

    def __init__(self) -> None:
        self.registered: dict[str, tuple[Path, str]] = {}

    def __call__(self, plan: Mapping[str, Any], allocation: Mapping[str, Any],
                 session_ref: str, session_directory: Path) -> Mapping[str, Any]:
        session_directory.mkdir(parents=True, exist_ok=True)
        return {"session_ref": session_ref, "status": "completed"}

    def observe(self, confirmation: Mapping[str, Any]) -> str:
        return str(confirmation.get("status", "absent"))

    def recover_launch(self, plan: Mapping[str, Any], allocation: Mapping[str, Any],
                       session_ref: str, session_directory: Path) -> Mapping[str, Any] | None:
        return None

    def collect_runner_receipt(self, confirmation: Mapping[str, Any],
                               plan: Mapping[str, Any]) -> Mapping[str, Any]:
        return dict(confirmation)

    def publish_run_artifact(self, confirmation: Mapping[str, Any],
                             plan: Mapping[str, Any], receipt: Mapping[str, Any]) -> str:
        return "runtime-run-" + uuid.uuid4().hex

    def retry_action(self, plan: Mapping[str, Any], receipt: Mapping[str, Any],
                     next_sequence: int) -> str | None:
        return None

    def start_retry(self, plan: Mapping[str, Any], allocation: Mapping[str, Any],
                    session_ref: str, session_directory: Path, *, run_id: str,
                    sequence: int, action: str) -> Mapping[str, Any]:
        return self(plan, allocation, session_ref, session_directory)

    def recover_retry(self, plan: Mapping[str, Any], allocation: Mapping[str, Any],
                      session_ref: str, session_directory: Path, *, run_id: str,
                      sequence: int, action: str) -> Mapping[str, Any] | None:
        return None

    def register_artifact(self, artifact_id: str, path: Path, kind: str) -> None:
        self.registered[str(artifact_id)] = (Path(path), str(kind))


class MinimalSimulationAdapter:
    """Neutral package materializer for the minimal-runtime example.

    The bytes intentionally describe only generic input data.  A real adapter
    should replace this payload with its own package format.
    """

    def __init__(self, entry: Mapping[str, Any]):
        self.adapter_id = str(entry.get("adapter_id", "minimal-simulation"))
        cfg = entry.get("config", {})
        self.package_dir = Path(cfg.get("package_dir", "examples/minimal-runtime/.runtime/packages")).resolve()
        self.qualifier_revision = "sha256:" + hashlib.sha256(
            self.adapter_id.encode("utf-8")
        ).hexdigest()

    def build_gateway(self, context: Mapping[str, Any]) -> _MinimalGateway:
        return _MinimalGateway()

    def materialize_package(self, evaluation_input: Mapping[str, Any],
                            task: Mapping[str, Any]) -> dict[str, str]:
        self.package_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "format": "minimal-runtime-neutral-input-v1",
            "evaluation_input": dict(evaluation_input),
            "task": dict(task),
            "note": "Placeholder package bytes; no solver-specific deck syntax.",
        }
        raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        revision = "sha256:" + hashlib.sha256(raw).hexdigest()
        artifact_id = self.adapter_id + ".package"
        path = self.package_dir / (revision.removeprefix("sha256:") + ".pkg")
        path.write_bytes(raw)
        return {"artifact_id": artifact_id, "revision": revision, "path": str(path)}

    def validate_package(self, context: Mapping[str, Any], task: Mapping[str, Any],
                         preparation: Mapping[str, Any], package: Mapping[str, str]) -> None:
        path = Path(package["path"])
        if not path.is_file():
            raise FileNotFoundError(path)

    def qualify(self, middleware: Any, attempt_id: str,
                context: Mapping[str, Any]) -> Mapping[str, Any]:
        """Qualify one completed attempt against the problem metric schema.

        The runtime-neutral fake reports a single generic finite metric so the
        whole queued -> planned -> running -> qualifying -> qualified chain is
        exercised through the real qualification contract.
        # TODO(adapter): compute real metrics from the collected run artifacts.
        """
        evaluation_input = middleware.get_evaluation_input(
            context["evaluation_id"]
        )
        problem = evaluation_input["problem"]
        evidence = tuple(sorted(context["artifact_ids"]))
        metrics = {"generic_figure_of_merit": 1.0}
        return make_qualification_report(
            evaluation_id=context["evaluation_id"],
            candidate_id=context["candidate_id"],
            attempt_ids=context["attempt_ids"],
            status="qualified",
            qualifier_revision=self.qualifier_revision,
            metric_schema_revision=problem["metric_schema_revision"],
            metrics=metrics,
            evidence_artifact_ids=evidence,
        )


def simulation_adapter_factory(entry: Mapping[str, Any]) -> MinimalSimulationAdapter:
    return MinimalSimulationAdapter(entry)

def resource_monitor_factory(entry: Mapping[str, Any]) -> FixedQuotaResourceMonitor:
    return FixedQuotaResourceMonitor(entry)

def worker_factory(entry: Mapping[str, Any]) -> MinimalWorker:
    return MinimalWorker(entry)
