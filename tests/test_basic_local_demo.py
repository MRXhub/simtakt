#!/usr/bin/env python3
"""End-to-end tests for the basic-local reference adapter and demo script.

These tests are fully self-contained: they exercise the local worker/gateway
protocol directly and run ``run_demo.py`` end-to-end in-process, without any
interactive input or external solver.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = REPO_ROOT / "examples" / "basic-local"


@contextmanager
def _example_modules():
    """Load same-named example modules without changing the process import path."""
    saved = {name: sys.modules.get(name) for name in ("local_adapter", "run_demo")}
    try:
        spec = importlib.util.spec_from_file_location(
            "_basic_local_local_adapter", EXAMPLE_DIR / "local_adapter.py"
        )
        adapter = importlib.util.module_from_spec(spec)
        sys.modules["local_adapter"] = adapter
        assert spec.loader is not None
        spec.loader.exec_module(adapter)
        spec = importlib.util.spec_from_file_location(
            "_basic_local_run_demo", EXAMPLE_DIR / "run_demo.py"
        )
        demo = importlib.util.module_from_spec(spec)
        sys.modules["_basic_local_run_demo"] = demo
        sys.modules["run_demo"] = demo
        assert spec.loader is not None
        spec.loader.exec_module(demo)
        yield adapter, demo
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        sys.modules.pop("_basic_local_run_demo", None)


with _example_modules() as (_local_adapter, _run_demo):
    LocalGateway = _local_adapter.LocalGateway
    LocalWorker = _local_adapter.LocalWorker
    BuiltinTargetCatalog = _run_demo.BuiltinTargetCatalog
    SqliteControlStore = _run_demo.SqliteControlStore
    dispatch_jobs = _run_demo.dispatch_jobs
    make_simulation_session_plan_fixture = _run_demo.make_simulation_session_plan_fixture

from control_plane.simulation.session_contracts import validate_simulation_session_result

class LocalAdapterProtocolTests(unittest.TestCase):
    def test_gateway_runs_a_short_local_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gateway = LocalGateway(
                artifact_root=root / "artifacts", work_delay_seconds=0.0
            )
            plan = make_simulation_session_plan_fixture()
            confirmation = gateway(
                plan,
                {"processors": 2, "run_id": "20260826-120000-001"},
                "local-session-1",
                root / "session-local-session-1",
            )
            self.assertEqual(gateway.observe(confirmation), "completed")
            self.assertEqual(confirmation["computed_value"], 5)
            self.assertTrue((root / "session-local-session-1" / "local-result.json").is_file())
            self.assertEqual(gateway.launch_confirmation_kind, "local-short-job-confirmed")

    def test_worker_dispatch_publish_collect_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = LocalWorker(root / "sessions")
            plan = make_simulation_session_plan_fixture()
            allocation = {
                "session_ref": "local-session-1",
                "run_id": "20260826-120000-001",
                "target_id": plan["target_id"],
                "processors": 2,
                "memory_bytes": 4 * 1024**3,
            }
            worker.start_session(plan, allocation, "local-session-1")
            self.assertIn(
                worker.observe_session("local-session-1"), {"running", "completed"}
            )
            result, artifact_id = worker.collect_session("local-session-1")
            self.assertEqual(result["status"], "completed")
            self.assertTrue(artifact_id.startswith("artifact:"))
            # The session result is contract-valid.
            self.assertEqual(
                validate_simulation_session_result(result), result
            )


class RunDemoEndToEndTests(unittest.TestCase):
    def test_run_demo_dispatch_jobs_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            control_store = SqliteControlStore(root / "control.sqlite")
            worker = LocalWorker(root / "sessions")
            try:
                rows = dispatch_jobs(
                    worker=worker,
                    control_store=control_store,
                    target_catalog=BuiltinTargetCatalog(),
                    job_count=2,
                )
                self.assertEqual(len(rows), 2)
                self.assertTrue(all(r["observation"] == "completed" for r in rows))
                self.assertTrue(all(r["artifact_id"] for r in rows))
                self.assertEqual(control_store.summary(), rows)
            finally:
                control_store.close()

    def test_run_demo_main_exits_zero(self) -> None:
        main = _run_demo.main

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--jobs", "2"])
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("2/2 local jobs completed", output)
        self.assertIn("dispatch summary", output)
    def test_same_process_example_loads_are_not_crosswired(self) -> None:
        """Distinct run_demo files remain distinct despite identical module names."""
        server_dir = REPO_ROOT / "examples" / "adapter-server-session"
        saved = {name: sys.modules.get(name) for name in ("fake_server", "session_adapter", "run_demo")}
        try:
            spec = importlib.util.spec_from_file_location(
                "_server_fake_for_isolation", server_dir / "fake_server.py"
            )
            fake = importlib.util.module_from_spec(spec)
            sys.modules["_server_fake_for_isolation"] = fake
            sys.modules["fake_server"] = fake
            assert spec.loader is not None
            spec.loader.exec_module(fake)
            session_spec = importlib.util.spec_from_file_location(
                "_session_for_isolation", server_dir / "session_adapter.py"
            )
            _session_for_isolation = importlib.util.module_from_spec(session_spec)
            sys.modules["_session_for_isolation"] = _session_for_isolation
            sys.modules["session_adapter"] = _session_for_isolation
            assert session_spec.loader is not None
            session_spec.loader.exec_module(_session_for_isolation)
            spec = importlib.util.spec_from_file_location(
                "_server_run_for_isolation", server_dir / "run_demo.py"
            )
            server_demo = importlib.util.module_from_spec(spec)
            sys.modules["run_demo"] = server_demo
            assert spec.loader is not None
            sys.modules["session_adapter"] = _session_for_isolation
            spec.loader.exec_module(server_demo)
            self.assertEqual(_run_demo.__file__, str(EXAMPLE_DIR / "run_demo.py"))
            self.assertEqual(server_demo.__file__, str(server_dir / "run_demo.py"))
            self.assertNotEqual(_run_demo.__file__, server_demo.__file__)
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
            sys.modules.pop("_server_fake_for_isolation", None)
            sys.modules.pop("_server_run_for_isolation", None)
            sys.modules.pop("_session_for_isolation", None)



if __name__ == "__main__":
    unittest.main()
