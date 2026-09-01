"""Reference local-process adapter; intentionally explicit about OS process semantics."""
from __future__ import annotations
import json, os, signal, subprocess, sys, time, uuid
from pathlib import Path
from typing import Any, Mapping
from control_plane.simulation.session_contracts import make_simulation_session_result, make_solver_run_record
from control_plane.simulation.worker import SessionStartFailure

class SimulationWorker:
    def __init__(self) -> None:
        self._meta: dict[str, dict[str, Any]] = {}
        self._procs: dict[str, subprocess.Popen] = {}
        self._plans: dict[str, Mapping[str, Any]] = {}

    def _root(self, allocation: Mapping[str, Any]) -> Path:
        value = allocation.get("workspace_root", allocation.get("remote_workspace_root"))
        if not value: raise ValueError("allocation.workspace_root is required")
        root = Path(str(value)); root.mkdir(parents=True, exist_ok=True); return root

    def _meta_path(self, root: Path) -> Path: return root / "local-process-bindings.json"
    def _save(self, root: Path) -> None:
        tmp = self._meta_path(root).with_suffix(".tmp")
        tmp.write_text(json.dumps(self._meta, sort_keys=True) + "\n", encoding="utf-8"); tmp.replace(self._meta_path(root))
    def _load(self, root: Path) -> None:
        try: self._meta.update(json.loads(self._meta_path(root).read_text(encoding="utf-8")))
        except (OSError, ValueError, UnicodeDecodeError): pass
    def _alive(self, pid: int) -> bool:
        try: os.kill(pid, 0); return True
        except (OSError, ProcessLookupError): return False

    def start_session(self, plan: Mapping[str, Any], allocation: Mapping[str, Any], session_ref: str) -> None:
        ref = str(session_ref)
        try:
            root = self._root(allocation)
            if not plan.get("plan_id") or not plan.get("attempt_id"): raise ValueError("plan_id and attempt_id are required")
            mode = str(allocation.get("mode", plan.get("mode", "normal")))
            token = uuid.uuid4().hex
            log = root / "solver.stdout.log"
            # TODO(command construction): decide executable path, argument order, and
            # whether processors belong on CLI or input file. Question: what does the
            # real solver contract require? Wrong choice silently changes the run.
            argv = [sys.executable, str(Path(__file__).with_name("fake_solver.py")), "--workspace", str(root), "--token", token, "--mode", mode]
            stream = log.open("wb")
            kwargs: dict[str, Any] = {"stdout": stream, "stderr": subprocess.STDOUT, "cwd": str(root)}
            if os.name == "nt": kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else: kwargs["start_new_session"] = True
            proc = subprocess.Popen(argv, **kwargs); stream.close()
        except (OSError, ValueError) as exc:
            raise SessionStartFailure("preflight_failed", "deterministic", str(exc)) from exc
        record = {"pid": proc.pid, "token": token, "log_path": str(root / "solver.log"), "stdout_path": str(log), "root": str(root), "mode": mode}
        self._meta[ref] = record; self._procs[ref] = proc; self._plans[ref] = plan
        try: self._save(root)
        except OSError as exc: raise SessionStartFailure("indeterminate", "persistence", "process started but binding was not persisted") from exc

    def resume_session(self, plan: Mapping[str, Any], allocation: Mapping[str, Any], session_ref: str) -> None:
        ref = str(session_ref); root = self._root(allocation); self._load(root)
        item = self._meta.get(ref)
        if not item: raise SessionStartFailure("indeterminate", "binding", "no durable binding")
        pidfile = Path(item["root"]) / "solver.pid.json"
        try: identity = json.loads(pidfile.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError): raise SessionStartFailure("indeterminate", "identity", "PID file unavailable")
        if identity.get("pid") != item["pid"] or identity.get("token") != item["token"] or not self._alive(item["pid"]):
            raise SessionStartFailure("indeterminate", "identity", "PID/token mismatch or process absent")
        self._plans[ref] = plan

    def observe_session(self, session_ref: str) -> str:
        item = self._meta.get(str(session_ref))
        if not item: return "indeterminate"
        pidfile = Path(item["root"]) / "solver.pid.json"
        try: identity = json.loads(pidfile.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError): return "indeterminate"
        if self._procs.get(str(session_ref)) is not None and self._procs[str(session_ref)].poll() is not None: pass
        proc = self._procs.get(str(session_ref))
        if proc is not None and proc.poll() is None: return "running"
        code = proc.poll() if proc else None
        # TODO(convergence criterion): decide production marker file/text/encoding.
        # Question: is CONVERGED: authoritative? Wrong choice mislabels divergence.
        try: text = Path(item["log_path"]).read_bytes().decode("utf-8", errors="replace")
        except OSError: return "indeterminate"
        return "completed" if code == 0 and "CONVERGED:" in text else "indeterminate"

    def terminate_session(self, session_ref: str) -> str:
        ref = str(session_ref); item = self._meta.get(ref)
        if not item: return "absent"
        pid = int(item["pid"])
        try:
            if os.name == "nt": subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            else:
                os.killpg(pid, signal.SIGTERM)
                deadline = time.monotonic() + 2
                while self._alive(pid) and time.monotonic() < deadline: time.sleep(.05)
                if self._alive(pid): os.killpg(pid, signal.SIGKILL)
            deadline = time.monotonic() + 5
            while self._alive(pid) and time.monotonic() < deadline: time.sleep(.05)
            child_file = Path(item["root"]) / "solver.child.pid.json"
            child = json.loads(child_file.read_text(encoding="utf-8")).get("pid") if child_file.exists() else None
            parent_alive = self._alive(pid) and (self._procs.get(ref) is None or self._procs[ref].poll() is None)
            if parent_alive or (child and self._alive(int(child))): return "indeterminate"
            return "terminated"
        except (OSError, ValueError, json.JSONDecodeError): return "indeterminate"

    def collect_session(self, session_ref: str) -> tuple[Mapping[str, Any], str]:
        ref = str(session_ref); item = self._meta[ref]; plan = self._plans[ref]; status = self.observe_session(ref)
        completed = status == "completed"; code = self._procs.get(ref).returncode if ref in self._procs else None
        # TODO(result extraction): map result.json into SolverRunRecord/evidence artifact.
        # Question: which schema and artifact store? Wrong extraction exposes stale data.
        package = plan.get("base_package", {"artifact_id": "artifact.local-package", "revision": "sha256:" + "0" * 64})
        # artifact. Question: which schema and artifact store? Wrong extraction can
        # expose stale or incomplete numerical output.
        run = make_solver_run_record(plan_id=plan["plan_id"], sequence=1, run_id=ref, package_artifact_id=package["artifact_id"], package_revision=package["revision"], numerical_profile_revision=plan.get("recovery_profile_revision", "sha256:" + "0" * 64), action="initial", status="completed" if completed else "failed", exit_code=0 if completed else code, artifact_ids=["artifact.local.result"] if completed else [])
        result = make_simulation_session_result(plan_id=plan["plan_id"], attempt_id=plan["attempt_id"], session_ref=ref, status="completed" if completed else "indeterminate", solver_run_record_ids=[run["record_id"]], journal_artifact_id="artifact.local.journal", evidence_artifact_ids=["artifact.local.result"] if completed else [], terminal_cause=None if completed else "solver-not-converged")
        return result, item["log_path"]

    terminate = terminate_session
