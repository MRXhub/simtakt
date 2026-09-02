"""Reference local-process adapter; intentionally explicit about OS process semantics."""
from __future__ import annotations
import json, os, signal, subprocess, sys, time, uuid
from pathlib import Path
from typing import Any, Mapping
from control_plane.simulation.session_contracts import make_simulation_session_result, make_solver_run_record
from control_plane.simulation.worker import SessionStartFailure

class _ResultWithRunRecord(dict):
    """Contract result plus the emitted record for demonstration consumers."""

    def __init__(self, result: Mapping[str, Any], run: Mapping[str, Any]) -> None:
        super().__init__(result)
        self._run = run

    def __getitem__(self, key: str) -> Any:
        if key == "solver_run_record":
            return self._run
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        if key == "solver_run_record":
            return self._run
        return super().get(key, default)


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
    def _alive(self, pid: int) -> bool | None:
        """Return True/False for liveness, or None when probing is indeterminate."""
        if os.name == "nt":
            try:
                completed = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                    timeout=15,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            if completed.returncode != 0:
                return None
            output = (completed.stdout or b"").decode("mbcs", errors="replace")
            for row in output.splitlines():
                fields = row.strip().split(",")
                if len(fields) >= 2 and fields[1].strip().strip('"') == str(pid):
                    return True
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return None
        except OSError:
            return False

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
        alive = self._alive(int(item["pid"]))
        if identity.get("pid") != item["pid"] or identity.get("token") != item["token"] or alive is not True:
            raise SessionStartFailure("indeterminate", "identity", "PID/token mismatch or process absent")
        self._plans[ref] = plan

    def observe_session(self, session_ref: str) -> str:
        ref = str(session_ref); item = self._meta.get(ref)
        if not item: return "indeterminate"
        pidfile = Path(item["root"]) / "solver.pid.json"
        try: identity = json.loads(pidfile.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError): return "indeterminate"
        if identity.get("pid") != item["pid"] or identity.get("token") != item["token"]:
            return "indeterminate"
        alive = self._alive(int(item["pid"]))
        if alive is None: return "indeterminate"
        proc = self._procs.get(ref)
        if alive: return "running"
        code = proc.poll() if proc else None
        try: text = Path(item["log_path"]).read_bytes().decode("utf-8", errors="replace")
        except OSError: return "indeterminate"
        # A clean process exit without the convergence marker is deterministic
        # solver exhaustion, not an uncertain adapter observation.
        return "completed" if code == 0 else "indeterminate"

    def terminate_session(self, session_ref: str) -> str:
        ref = str(session_ref); item = self._meta.get(ref)
        if not item: return "absent"
        pid = int(item["pid"])
        try:
            pidfile = Path(item["root"]) / "solver.pid.json"
            identity = json.loads(pidfile.read_text(encoding="utf-8"))
            if identity.get("pid") != item["pid"] or identity.get("token") != item["token"]:
                return "indeterminate"
            initial_alive = self._alive(pid)
            if initial_alive is None: return "indeterminate"
            if not initial_alive: return "terminated"
            if os.name == "nt":
                try:
                    subprocess.run(
                        ["taskkill", "/T", "/F", "/PID", str(pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=15,
                    )
                except subprocess.TimeoutExpired:
                    return "indeterminate"
            else:
                os.killpg(pid, signal.SIGTERM)
                deadline = time.monotonic() + 2
                while self._alive(pid) is True and time.monotonic() < deadline: time.sleep(.05)
                if self._alive(pid) is True: os.killpg(pid, signal.SIGKILL)
            deadline = time.monotonic() + 5
            while self._alive(pid) is True and time.monotonic() < deadline: time.sleep(.05)
            child_file = Path(item["root"]) / "solver.child.pid.json"
            child = json.loads(child_file.read_text(encoding="utf-8")).get("pid") if child_file.exists() else None
            parent_alive = self._alive(pid)
            child_alive = self._alive(int(child)) if child else False
            if parent_alive is None or child_alive is None: return "indeterminate"
            if parent_alive or child_alive: return "indeterminate"
            return "terminated"
        except (OSError, ValueError, json.JSONDecodeError): return "indeterminate"

    def collect_session(self, session_ref: str) -> tuple[Mapping[str, Any], str]:
        ref = str(session_ref); item = self._meta[ref]; plan = self._plans[ref]; observed = self.observe_session(ref)
        code = self._procs.get(ref).returncode if ref in self._procs else None
        try: text = Path(item["log_path"]).read_bytes().decode("utf-8", errors="replace")
        except OSError: text = ""
        timing_seconds: float | None = None
        try:
            timing = json.loads((Path(item["root"]) / "solver-timing.json").read_text(encoding="utf-8"))
            started = int(timing["solve_started_ns"]); finished = int(timing["solve_finished_ns"])
            if finished >= started: timing_seconds = (finished - started) / 1_000_000_000
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            # Missing/corrupt solver timing is not approximated with attempt lifetime.
            timing_seconds = None
        converged = observed == "completed" and "CONVERGED:" in text
        exhausted = observed == "completed" and not converged
        package = plan.get("base_package", {"artifact_id": "artifact.local-package", "revision": "sha256:" + "0" * 64})
        run = make_solver_run_record(plan_id=plan["plan_id"], sequence=1, run_id=ref, package_artifact_id=package["artifact_id"], package_revision=package["revision"], numerical_profile_revision=plan.get("recovery_profile_revision", "sha256:" + "0" * 64), action="initial", status="completed" if converged else "failed", exit_code=0 if converged or exhausted else code, artifact_ids=["artifact.local.result"] if converged else [], wall_seconds=timing_seconds)
        result_status = "completed" if converged else "exhausted" if exhausted else "indeterminate"
        result = make_simulation_session_result(plan_id=plan["plan_id"], attempt_id=plan["attempt_id"], session_ref=ref, status=result_status, solver_run_record_ids=[run["record_id"]], solver_run_records=[run], journal_artifact_id="artifact.local.journal", evidence_artifact_ids=["artifact.local.result"] if converged else [], terminal_cause=None if converged else "solver-not-converged")
        return _ResultWithRunRecord(result, run), item["log_path"]


    terminate = terminate_session
