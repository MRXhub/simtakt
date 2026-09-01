#!/usr/bin/env python3
"""End-to-end tests for the basic-local reference adapter and demo script.

These tests are fully self-contained: they exercise the local worker/gateway
protocol directly and run ``run_demo.py`` end-to-end in-process, without any
interactive input or external solver.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = REPO_ROOT / "examples" / "basic-local"
for _path in (str(REPO_ROOT), str(EXAMPLE_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from local_adapter import LocalGateway, LocalWorker  # noqa: E402
from run_demo import (  # noqa: E402
    BuiltinTargetCatalog,
    SqliteControlStore,
    dispatch_jobs,
    make_simulation_session_plan_fixture,
)

from control_plane.simulation.session_contracts import (  # noqa: E402
    validate_simulation_session_result,
)


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
        from run_demo import main

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--jobs", "2"])
        self.assertEqual(code, 0)
        output = buffer.getvalue()
        self.assertIn("2/2 local jobs completed", output)
        self.assertIn("dispatch summary", output)


if __name__ == "__main__":
    unittest.main()
