from __future__ import annotations

import csv
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
for p in (ROOT, EXAMPLE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
# Discovery may already have imported same-named modules from another example.
sys.modules.pop("adapter", None)
sys.modules.pop("run_demo", None)
from adapter import SimulationWorker
from run_demo import plan

# Do not leave this example's run_demo in the global module cache: discovery also
# imports the other examples' identically named run_demo modules.
sys.modules.pop("run_demo", None)
sys.path.remove(str(EXAMPLE))
from control_plane.simulation.session_contracts import validate_simulation_session_result
VALID_STATES = {"running", "completed", "failed", "unreachable", "indeterminate"}


def process_alive(pid: int) -> bool:
    """Probe process existence without the destructive Windows os.kill(pid, 0)."""
    if os.name == "nt":
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        output = (completed.stdout or b"").decode("mbcs", errors="replace")
        for row in csv.reader(output.splitlines()):
            if len(row) >= 2 and row[1].strip() == str(pid):
                return True
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
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
            if pid and process_alive(int(pid)) and os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, check=False)
            elif pid and process_alive(int(pid)):
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
        self.assertTrue(process_alive(int(identity["pid"])))

    def test_observe_probe_has_no_side_effect(self):
        ref = self.start("hang")
        pid = self.worker._procs[ref].pid
        self.wait_for(lambda: self.worker.observe_session(ref) == "running")
        for _ in range(5):
            self.assertEqual(self.worker.observe_session(ref), "running")
        self.assertTrue(process_alive(pid))

    def test_terminate_kills_parent_and_child_process_tree(self):
        ref = self.start("tree")
        child_pid = int(self.wait_for(lambda: (self.root / "solver.child.pid").read_text(encoding="ascii") if (self.root / "solver.child.pid").exists() else None))
        parent_pid = self.worker._procs[ref].pid
        self.assertTrue(process_alive(parent_pid))
        self.assertTrue(process_alive(child_pid))
        self.assertEqual(self.worker.terminate_session(ref), "terminated")
        self.assertFalse(process_alive(parent_pid))
        self.assertFalse(process_alive(child_pid))

    def test_pid_token_mismatch_is_not_treated_as_owned_process(self):
        ref = self.start("hang")
        pid_file = self.root / "solver.pid.json"
        self.wait_for(lambda: pid_file.exists())
        identity = json.loads(pid_file.read_text(encoding="utf-8"))
        identity["token"] = "not-the-launch-token"
        pid_file.write_text(json.dumps(identity), encoding="utf-8")
        # A mismatched identity must not make an unrelated/live process appear owned.
        self.assertNotEqual(self.worker.observe_session(ref), "running")
        self.assertTrue(process_alive(int(identity["pid"])))

    def test_resume_is_idempotent_and_does_not_spawn_second_process(self):
        ref = self.start("hang")
        pid_file = self.root / "solver.pid.json"
        self.wait_for(lambda: pid_file.exists())
        original_pid = self.worker._procs[ref].pid
        for _ in range(3):
            self.worker.resume_session(plan(), {"workspace_root": str(self.root), "mode": "hang"}, ref)
        self.assertEqual(self.worker._procs[ref].pid, original_pid)
        self.assertTrue(process_alive(original_pid))

    def test_non_utf8_log_does_not_crash_observe(self):
        ref = self.start("nonutf8")
        state = self.wait_for(lambda: self.worker.observe_session(ref) if self.worker._procs[ref].poll() is not None else None)
        self.assertIn(state, VALID_STATES)

    def test_hanging_process_is_terminated(self):
        ref = self.start("hang")
        self.wait_for(lambda: "running" if self.worker.observe_session(ref) == "running" else None)
        pid = self.worker._procs[ref].pid
        self.assertEqual(self.worker.terminate_session(ref), "terminated")
        self.assertFalse(process_alive(pid))


if __name__ == "__main__":
    unittest.main()
