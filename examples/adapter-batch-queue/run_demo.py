from __future__ import annotations
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from adapter import BatchQueueWorker
from fake_queue import FakeBatchQueue

def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        queue = FakeBatchQueue(); worker = BatchQueueWorker(queue)
        from control_plane.simulation.session_contracts import make_simulation_session_plan
        plan = make_simulation_session_plan(
            attempt_id="attempt:00000000-0000-4000-8000-000000000001",
            evaluation_id="evaluation:00000000-0000-4000-8000-000000000002",
            candidate_id="candidate:sha256:" + "a" * 64, simulation_proxy="batch-demo",
            recovery_profile_revision="sha256:" + "b" * 64, base_package_artifact_id="artifact:pkg",
            base_package_revision="sha256:" + "c" * 64, task_id="task:demo", target_id="target:demo",
            authorization_id="artifact:auth", authorization_revision="sha256:" + "d" * 64,
            requested_processors=4, command_timeout_seconds=60,
            max_solver_runs=1, max_wall_seconds=60)
        allocation = {"remote_workspace_root": str(Path(tmp) / "workspace"), "processors": 4}
        worker.start_session(plan, allocation, "session:demo")
        job = worker.job_id_for("session:demo")
        print("submitted", job, "observe=", worker.observe_session("session:demo"))
        queue.complete(job, 0, elapsed="00:00:00.250")  # active disappears; history remains
        print("after completion observe=", worker.observe_session("session:demo"))
        result, artifact = worker.collect_session("session:demo")
        record = result["solver_run_record"]
        seconds = record["wall_seconds"]
        if seconds is None:
            print(f"result status={result['status']} elapsed_wall_seconds=unavailable artifact={artifact}")
        else:
            assert seconds > 0
            print(f"result status={result['status']} elapsed_wall_seconds={seconds:.3f} artifact={artifact}")

if __name__ == "__main__":
    main()
