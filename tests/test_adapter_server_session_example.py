from __future__ import annotations
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "adapter-server-session"
for p in (ROOT, EXAMPLE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
from fake_server import FakeServer
from run_demo import make_plan
from session_adapter import ServerSessionWorker
import sys as _sys
_sys.modules.pop("run_demo", None)  # do not shadow basic-local's run_demo in discovery
_sys.path.remove(str(EXAMPLE))
from control_plane.simulation.session_contracts import validate_simulation_session_result

class ServerSessionExampleTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.worker = ServerSessionWorker(self.server)
        self.plan = make_plan()
        self.allocation = {"run_id": "run-1"}

    def test_normal_lifecycle(self):
        self.worker.start_session(self.plan, self.allocation, "s1")
        self.assertEqual(self.worker.observe_session("s1"), "running")
        self.assertEqual(self.worker.observe_session("s1"), "completed")
        result, artifact = self.worker.collect_session("s1")
        self.assertEqual(validate_simulation_session_result(result), result)
        self.assertTrue(artifact.startswith("artifact:"))
        self.assertEqual(self.worker.terminate_session("s1"), "terminated")
        self.assertEqual(self.server.license_count, 0)

    def test_resume_does_not_create_second_session(self):
        self.worker.start_session(self.plan, self.allocation, "s1")
        self.worker.resume_session(self.plan, self.allocation, "s1")
        self.worker.resume_session(self.plan, self.allocation, "s1")
        self.assertEqual(self.server.create_count, 1)

    def test_lost_connection_is_unreachable(self):
        self.worker.start_session(self.plan, self.allocation, "s1")
        self.server.invalidate_connections()
        self.assertEqual(self.worker.observe_session("s1"), "unreachable")
        self.assertIn("s1", self.server.sessions)

    def test_disconnect_without_terminate_leaks_license(self):
        self.worker.start_session(self.plan, self.allocation, "s1")
        self.server.invalidate_connections()
        # Simulating a dead transport is not explicit shutdown.
        self.assertEqual(self.server.license_count, 1)

if __name__ == "__main__":
    unittest.main()
