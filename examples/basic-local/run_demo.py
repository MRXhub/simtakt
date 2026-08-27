#!/usr/bin/env python3
"""End-to-end local dispatch demo for the basic-local reference adapter.

The demo stands up an in-memory SQLite control store, a built-in
TargetCatalog/ControlStore fixture, and a pure local scheduler that dispatches a
few short local jobs through the reference worker/gateway adapter.  Each step is
printed to the terminal and recorded in the control store, and the process
exits 0 when every dispatched session completes.

Run from the repository root:

    python examples/basic-local/run_demo.py

To view the same control plane in a browser, start the self-contained web
console in demo mode:

    python -m control_plane.web.status_server --demo
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_EXAMPLE_DIR = Path(__file__).resolve().parent
if str(_EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(_EXAMPLE_DIR))

from local_adapter import LocalWorker  # noqa: E402

from control_plane.evaluation.scheduling import (  # noqa: E402
    make_resource_allocation,
    schedule_legacy_v1,
    validate_scheduling_decision,
)
from control_plane.simulation.session_contracts import (  # noqa: E402
    make_simulation_session_plan,
)

DEFAULT_TARGET = "local.target-main"
RUN_ID_DATE = "20260826-120000"


class BuiltinTargetCatalog:
    """A TargetCatalog fixture returning a single ready local target."""

    def read_targets(self, project_root: Path | str) -> Sequence[Mapping[str, object]]:
        return [
            {
                "target_id": DEFAULT_TARGET,
                "status": "ready",
                "processors": 4,
                "memory_bytes": 8 * 1024**3,
            }
        ]


class SqliteControlStore:
    """An in-memory/on-disk SQLite ControlStore fixture.

    Reads a fixed project-state view through the ControlStore port and records
    every dispatched local session in a ``local_dispatch`` table so the demo can
    summarize what the scheduler ran.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = str(db_path) if db_path is not None else ":memory:"
        self.connection = sqlite3.connect(self.db_path)
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS local_dispatch (
                session_ref TEXT PRIMARY KEY,
                attempt_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                processors INTEGER NOT NULL,
                observation TEXT NOT NULL,
                artifact_id TEXT
            )
            """
        )
        self.connection.commit()

    def read_project_state(self, project_root: Path | str) -> Mapping[str, object]:
        return {"project": "basic-local", "state": "ready", "source": "sqlite"}

    def record(
        self,
        *,
        session_ref: str,
        attempt_id: str,
        run_id: str,
        target_id: str,
        processors: int,
        observation: str,
        artifact_id: str | None,
    ) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO local_dispatch VALUES (?,?,?,?,?,?,?)",
            (
                session_ref,
                attempt_id,
                run_id,
                target_id,
                processors,
                observation,
                artifact_id,
            ),
        )
        self.connection.commit()

    def summary(self) -> list[Mapping[str, object]]:
        rows = self.connection.execute(
            "SELECT session_ref, attempt_id, run_id, target_id, "
            "processors, observation, artifact_id "
            "FROM local_dispatch ORDER BY run_id"
        ).fetchall()
        return [
            {
                "session_ref": row[0],
                "attempt_id": row[1],
                "run_id": row[2],
                "target_id": row[3],
                "processors": row[4],
                "observation": row[5],
                "artifact_id": row[6],
            }
            for row in rows
        ]

    def close(self) -> None:
        self.connection.close()


def make_simulation_session_plan_fixture(
    *,
    processors: int = 2,
    target_id: str = DEFAULT_TARGET,
) -> Mapping[str, object]:
    """Build a valid SimulationSessionPlan for the local adapter."""
    return make_simulation_session_plan(
        attempt_id=f"attempt:{uuid.uuid4()}",
        evaluation_id=f"evaluation:{uuid.uuid4()}",
        candidate_id="candidate:sha256:" + "a" * 64,
        simulation_proxy="local-short-job",
        recovery_profile_revision="sha256:" + "b" * 64,
        base_package_artifact_id="package.local",
        base_package_revision="sha256:" + "c" * 64,
        task_id="local-task",
        target_id=target_id,
        authorization_id="authorization.local",
        authorization_revision="sha256:" + "d" * 64,
        requested_processors=processors,
        command_timeout_seconds=600,
        max_solver_runs=3,
        max_wall_seconds=1800,
    )


def _resource_snapshot(target_id: str) -> Mapping[str, object]:
    return {
        "schema_version": 1,
        "snapshot_kind": "resource-snapshot",
        "snapshot_revision": "sha256:" + "1" * 64,
        "target_id": target_id,
        "status": "ready",
        "available_processors": 4,
        "available_memory_bytes": 8 * 1024**3,
        "default_request_memory_bytes": 4 * 1024**3,
        "observed_allocation_keys": [],
        "reasons": [],
        "created_at": "2026-08-26T11:59:00+00:00",
        "lock_held": True,
        "target_is_idle": True,
    }


def _candidate(attempt_id: str, target_id: str, processors: int) -> Mapping[str, object]:
    return {
        "attempt_id": attempt_id,
        "target_id": target_id,
        "requested_processors": processors,
        "requested_memory_bytes": 4 * 1024**3,
    }


def dispatch_jobs(
    *,
    worker: LocalWorker,
    control_store: SqliteControlStore,
    target_catalog: BuiltinTargetCatalog,
    job_count: int,
    print_fn: object = print,
) -> list[Mapping[str, object]]:
    """Schedule and dispatch ``job_count`` local jobs through the reference worker.

    Returns the control-store summary rows after every job completes.
    """
    targets = list(target_catalog.read_targets(REPO_ROOT))
    target_id = targets[0]["target_id"]

    completed: list[Mapping[str, object]] = []
    for index in range(1, job_count + 1):
        session_ref = f"local-session-{index}"
        attempt_id = f"attempt:{uuid.uuid4()}"
        run_id = f"{RUN_ID_DATE}-{index:03d}"
        processors = 1 + (index % 2)  # 2 then 1, keeps the demo small

        print_fn(f"[step {index}] scheduling {session_ref} (attempt={attempt_id})")
        decision = schedule_legacy_v1(
            [_candidate(attempt_id, target_id, processors)],
            [],
            _resource_snapshot(target_id),
        )
        validate_scheduling_decision(decision)
        if decision["action"] != "launch":
            raise RuntimeError(f"{session_ref}: scheduler refused to launch")

        plan = make_simulation_session_plan_fixture(
            processors=processors, target_id=target_id
        )
        allocation = make_resource_allocation(
            decision,
            session_ref=session_ref,
            run_id=run_id,
            remote_workspace_root="/local/work",
            decision_artifact_id="evidence.scheduling-decision.local",
            decision_artifact_path="data/local/decision.json",
        )

        print_fn(f"[step {index}] dispatching local job on {target_id}")
        worker.start_session(plan, allocation, session_ref)

        observation = worker.observe_session(session_ref)
        print_fn(f"[step {index}] observation: {observation}")
        if observation == "running":
            deadline = time.monotonic() + 15.0
            while observation == "running" and time.monotonic() < deadline:
                time.sleep(0.05)
                observation = worker.observe_session(session_ref)
            print_fn(f"[step {index}] observation after wait: {observation}")
        if observation != "completed":
            raise RuntimeError(f"{session_ref}: job did not complete")

        result, artifact_id = worker.collect_session(session_ref)
        print_fn(
            f"[step {index}] collected result status={result['status']} "
            f"artifact={artifact_id}"
        )

        control_store.record(
            session_ref=session_ref,
            attempt_id=attempt_id,
            run_id=run_id,
            target_id=target_id,
            processors=processors,
            observation=observation,
            artifact_id=artifact_id,
        )
        completed.append(control_store.summary()[-1])

    return completed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--jobs",
        type=int,
        default=3,
        help="number of local jobs to dispatch (default: 3)",
    )
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")

    work_root = Path(tempfile.mkdtemp(prefix="basic-local-"))
    print(f"control store: sqlite at {work_root / 'control.sqlite'}")
    control_store = SqliteControlStore(work_root / "control.sqlite")
    target_catalog = BuiltinTargetCatalog()

    worker = LocalWorker(work_root / "sessions")
    print(f"worker sessions root: {worker.sessions_root}")

    try:
        completed = dispatch_jobs(
            worker=worker,
            control_store=control_store,
            target_catalog=target_catalog,
            job_count=args.jobs,
        )
    finally:
        control_store.close()

    print("\n=== dispatch summary ===")
    for row in completed:
        print(
            f"  {row['run_id']}  {row['session_ref']}  "
            f"{row['target_id']}  processors={row['processors']}  "
            f"{row['observation']}  {row['artifact_id']}"
        )
    print(f"\n{len(completed)}/{args.jobs} local jobs completed (exit 0)")
    print(
        "web console: start it in demo mode with "
        "`python -m control_plane.web.status_server --demo`"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
