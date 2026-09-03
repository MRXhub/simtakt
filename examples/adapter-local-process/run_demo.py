"""Run normal, divergence, and process-tree termination demonstrations."""
from __future__ import annotations
import sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from control_plane.simulation.session_contracts import make_simulation_session_plan
from adapter import SimulationWorker

def plan():
    from uuid import uuid4
    z="sha256:"+"0"*64
    return make_simulation_session_plan(attempt_id="attempt:"+str(uuid4()),evaluation_id="evaluation:"+str(uuid4()),candidate_id="candidate:sha256:"+"1"*64,simulation_proxy="local",recovery_profile_revision=z,base_package_artifact_id="pkg",base_package_revision=z,task_id="task",target_id="target",authorization_id="auth",authorization_revision=z,requested_processors=1,command_timeout_seconds=10,max_solver_runs=1,max_wall_seconds=30)

def run(mode):
    root=Path(tempfile.mkdtemp(prefix="local-process-")); w=SimulationWorker(); ref="session-"+mode
    w.start_session(plan(),{"workspace_root":str(root),"mode":mode},ref)
    if mode=="tree":
        time.sleep(.4); child=int((root/"solver.child.pid").read_text()); out=w.terminate_session(ref); print(f"tree terminate={out}, child_alive={w._alive(child)}")
    else:
        while w._procs[ref].poll() is None: time.sleep(.05)
        result, _ = w.collect_session(ref)
        record = result["solver_run_record"]
        seconds = record["wall_seconds"]
        if seconds is None:
            print(f"{mode} measured_wall_seconds=unavailable")
        else:
            assert seconds > 0
            print(f"{mode} measured_wall_seconds={seconds:.9f}")
if __name__=="__main__": run("normal"); run("diverge"); run("tree")
