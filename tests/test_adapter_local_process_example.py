from __future__ import annotations

import csv
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "adapter-local-process"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_saved = {name: sys.modules.get(name) for name in ("adapter", "run_demo")}
try:
    _adapter = _load(EXAMPLE / "adapter.py", "_local_process_adapter")
    sys.modules["adapter"] = _adapter
    _run_demo = _load(EXAMPLE / "run_demo.py", "_local_process_run_demo")
finally:
    for _name, _module in _saved.items():
        if _module is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _module
    sys.modules.pop("_local_process_adapter", None)
    sys.modules.pop("_local_process_run_demo", None)

SimulationWorker = _adapter.SimulationWorker
plan = _run_demo.plan

from control_plane.simulation.session_contracts import validate_simulation_session_result
from control_plane.simulation.worker import SESSION_OBSERVATIONS
VALID_STATES = SESSION_OBSERVATIONS

def process_alive(pid: int) -> bool | None:
    """Probe process existence without the destructive Windows os.kill(pid, 0)."""
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        output = (completed.stdout or b"").decode("mbcs", errors="replace")
        for row in csv.reader(output.splitlines()):
            if len(row) >= 2 and row[1].strip() == str(pid):
                return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError:
        return False
    return True


class AdapterLocalProcessExampleTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="adapter-local-process-test-"))
        self.worker = SimulationWorker()
        self.refs: set[str] = set()

    def tearDown(self):
        # terminate_session performs tree cleanup; repeat direct PID cleanup as a
        # last-resort guard so a failed assertion cannot poison later tests.
        for ref in list(self.refs):
            try:
                self.worker.terminate_session(ref)
            except Exception:
                pass
        for pid_file in (self.root / "solver.pid.json", self.root / "solver.child.pid.json"):
            try:
                pid = json.loads(pid_file.read_text(encoding="utf-8")).get("pid")
            except (OSError, ValueError, UnicodeDecodeError):
                pid = None
            if pid and process_alive(int(pid)) is True and os.name == "nt":
                try:
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, check=False, timeout=15)
                except subprocess.TimeoutExpired:
                    pass
            elif pid and process_alive(int(pid)) is True:
                try:
                    os.kill(int(pid), 9)
                except OSError:
                    pass
        shutil.rmtree(self.root, ignore_errors=True)

    def start(self, mode: str) -> str:
        ref = "session-" + mode
        self.refs.add(ref)
        self.worker.start_session(plan(), {"workspace_root": str(self.root), "mode": mode}, ref)
        return ref

    def wait_for(self, predicate, timeout: float = 8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail("timed out waiting for fake solver")

    def test_normal_lifecycle(self):
        ref = self.start("normal")
        self.wait_for(lambda: self.worker.observe_session(ref) == "completed")
        result, artifact_id = self.worker.collect_session(ref)
        self.assertEqual(validate_simulation_session_result(result), result)
        self.assertTrue(str(artifact_id).endswith("solver.log"))

    def test_divergence_is_reported_as_exhausted(self):
        ref = self.start("diverge")
        self.wait_for(lambda: self.worker.observe_session(ref) if self.worker._procs[ref].poll() is not None else None)
        self.assertEqual(self.worker.observe_session(ref), "completed")
        result, _ = self.worker.collect_session(ref)
        self.assertEqual(result["status"], "exhausted")
        self.assertEqual(result["terminal_cause"], "solver-not-converged")
        self.assertEqual(validate_simulation_session_result(result), result)

    def test_terminate_refuses_mismatched_token(self):
        ref = self.start("hang")
        pid_file = self.root / "solver.pid.json"
        self.wait_for(lambda: pid_file.exists())
        identity = json.loads(pid_file.read_text(encoding="utf-8"))
        identity["token"] = "not-the-launch-token"
        pid_file.write_text(json.dumps(identity), encoding="utf-8")
        self.assertNotEqual(self.worker.terminate_session(ref), "terminated")
        self.assertIs(process_alive(int(identity["pid"])), True)

    def test_observe_probe_has_no_side_effect(self):
        ref = self.start("hang")
        pid = self.worker._procs[ref].pid
        self.wait_for(lambda: self.worker.observe_session(ref) == "running")
        for _ in range(5):
            self.assertEqual(self.worker.observe_session(ref), "running")
        self.assertIs(process_alive(pid), True)

    def test_terminate_kills_parent_and_child_process_tree(self):
        ref = self.start("tree")
        child_pid = int(self.wait_for(lambda: (self.root / "solver.child.pid").read_text(encoding="ascii") if (self.root / "solver.child.pid").exists() else None))
        parent_pid = self.worker._procs[ref].pid
        self.assertIs(process_alive(parent_pid), True)
        self.assertIs(process_alive(child_pid), True)
        self.assertEqual(self.worker.terminate_session(ref), "terminated")
        self.assertIs(process_alive(parent_pid), False)
        self.assertIs(process_alive(child_pid), False)

    def test_pid_token_mismatch_is_not_treated_as_owned_process(self):
        ref = self.start("hang")
        pid_file = self.root / "solver.pid.json"
        self.wait_for(lambda: pid_file.exists())
        identity = json.loads(pid_file.read_text(encoding="utf-8"))
        identity["token"] = "not-the-launch-token"
        pid_file.write_text(json.dumps(identity), encoding="utf-8")
        # A mismatched identity must not make an unrelated/live process appear owned.
        self.assertNotEqual(self.worker.observe_session(ref), "running")
        self.assertIs(process_alive(int(identity["pid"])), True)

    def test_resume_is_idempotent_and_does_not_spawn_second_process(self):
        ref = self.start("hang")
        pid_file = self.root / "solver.pid.json"
        self.wait_for(lambda: pid_file.exists())
        original_pid = self.worker._procs[ref].pid
        for _ in range(3):
            self.worker.resume_session(plan(), {"workspace_root": str(self.root), "mode": "hang"}, ref)
        self.assertEqual(self.worker._procs[ref].pid, original_pid)
        self.assertIs(process_alive(original_pid), True)

    def test_non_utf8_log_does_not_crash_observe(self):
        ref = self.start("nonutf8")
        state = self.wait_for(lambda: self.worker.observe_session(ref) if self.worker._procs[ref].poll() is not None else None)
        self.assertIn(state, VALID_STATES)

    def test_hanging_process_is_terminated(self):
        ref = self.start("hang")
        self.wait_for(lambda: "running" if self.worker.observe_session(ref) == "running" else None)
        pid = self.worker._procs[ref].pid
        self.assertEqual(self.worker.terminate_session(ref), "terminated")
        self.assertIs(process_alive(pid), False)


if __name__ == "__main__":
    unittest.main()
