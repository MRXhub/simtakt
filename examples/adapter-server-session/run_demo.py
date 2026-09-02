#!/usr/bin/env python3
from __future__ import annotations
import argparse
import sys
from pathlib import Path

# Permit direct execution from any working directory.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from fake_server import FakeServer
from session_adapter import ServerSessionWorker
from control_plane.simulation.session_contracts import make_simulation_session_plan

def make_plan():
    return make_simulation_session_plan(
        attempt_id="attempt:00000000-0000-0000-0000-000000000001",
        evaluation_id="evaluation:00000000-0000-0000-0000-000000000002",
        candidate_id="candidate:sha256:" + "a" * 64,
        simulation_proxy="adapter-server-session", recovery_profile_revision="sha256:" + "b" * 64,
        base_package_artifact_id="package", base_package_revision="sha256:" + "c" * 64,
        task_id="demo", target_id="fake-solver", authorization_id="auth",
        authorization_revision="sha256:" + "d" * 64, requested_processors=1,
        command_timeout_seconds=30, max_solver_runs=1, max_wall_seconds=60,
    )

def main(argv=None):
    parser = argparse.ArgumentParser(); parser.add_argument("--state", type=Path)
    parser.parse_args(argv)
    server = FakeServer(); worker = ServerSessionWorker(server)
    plan = make_plan(); allocation = {"run_id": "demo-run"}
    worker.start_session(plan, allocation, "demo-session")
    worker.resume_session(plan, allocation, "demo-session")
    while worker.observe_session("demo-session") != "completed":
        pass
    result, artifact = worker.collect_session("demo-session")
    record = result["solver_run_record"]
    duration = record["wall_seconds"]
    if duration is None:
        duration_text = "unavailable"
    else:
        assert duration > 0
        duration_text = str(duration)
    worker.terminate_session("demo-session")
    print(f"server session completed: {result['status']}; measured_wall_seconds={duration_text}; artifact={artifact}; licenses={server.license_count}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
