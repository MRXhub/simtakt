from __future__ import annotations
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "adapter-server-session"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module

_saved = {name: sys.modules.get(name) for name in ("fake_server", "session_adapter", "run_demo")}
try:
    _fake_server = _load(EXAMPLE / "fake_server.py", "_server_fake_server")
    sys.modules["fake_server"] = _fake_server
    _session_adapter = _load(EXAMPLE / "session_adapter.py", "_server_session_adapter")
    sys.modules["session_adapter"] = _session_adapter
    _run_demo = _load(EXAMPLE / "run_demo.py", "_server_run_demo")
finally:
    for _name, _module in _saved.items():
        if _module is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _module
    for _name in ("_server_fake_server", "_server_session_adapter", "_server_run_demo"):
        sys.modules.pop(_name, None)

FakeServer = _fake_server.FakeServer
make_plan = _run_demo.make_plan
ServerSessionWorker = _session_adapter.ServerSessionWorker

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
