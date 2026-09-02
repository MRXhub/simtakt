"""Build a disposable input package and run the governed runtime."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / ".runtime"
DATA = ROOT / "data"
sys.path.insert(0, str(ROOT.parent.parent))

from control_plane.core.evaluation_contracts import (
    make_candidate,
    make_evaluation_request,
    make_problem_definition,
)
from control_plane.evaluation.parameter_schema import make_parameter_schema
from control_plane.evaluation.service import EvaluationMiddleware
from control_plane.evaluation.control_plane import resolve_control_plane_database
from control_plane.data.sqlite_evaluation_repository import SQLiteEvaluationRepository


def _register_package() -> dict[str, str]:
    package = RUNTIME / "input-package"
    package.mkdir(parents=True, exist_ok=True)
    (package / "input.txt").write_text("minimal input\n", encoding="utf-8")
    manifest = {"schema_version": 1, "artifact_id": "package.minimal.input", "files": [{"path": "input.txt"}]}
    (package / "manifest.json").write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    revision = "sha256:" + hashlib.sha256((package / "manifest.json").read_bytes()).hexdigest()
    record = {"schema_version": 1, "record_kind": "artifact-catalog-shard", "artifact": {
        "artifact_id": "package.minimal.input", "kind": "input-package", "status": "active",
        "latest_revision": revision, "revisions": [{"revision": revision, "hash_scope": "package-manifest",
        "locations": [{"storage": "workspace", "role": "primary", "availability": "current",
        "path": ".runtime/input-package"}]}]}}
    records = ROOT / "records" / "artifacts"
    records.mkdir(parents=True, exist_ok=True)
    (records / "package.minimal.input.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return {"artifact_id": "package.minimal.input", "revision": revision}


def _unresolved_reason(evaluation_id: str) -> str | None:
    """Surface the stored reason when an evaluation fails to qualify."""
    try:
        with sqlite3.connect(resolve_control_plane_database(ROOT)) as connection:
            row = connection.execute(
                "SELECT payload_json FROM state_events "
                "WHERE aggregate_id = ? AND event_type = 'EvaluationUnresolved' "
                "ORDER BY sequence DESC LIMIT 1",
                (evaluation_id,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        payload = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None
    reason = payload.get("reason") if isinstance(payload, dict) else None
    return reason if isinstance(reason, str) and reason else None


def main() -> int:
    # Start from a clean state every run: .runtime/ holds the disposable
    # workspace and data/ holds the control-plane SQLite database.  A stale
    # data/ left by an earlier or interrupted run leaks queued/qualifying
    # evaluations (keyed by a deterministic idempotency key) and parameter
    # schema revisions, so the demo is not hermetic otherwise.
    shutil.rmtree(RUNTIME, ignore_errors=True)
    shutil.rmtree(DATA, ignore_errors=True)
    try:
        RUNTIME.mkdir(parents=True)
        identity = _register_package()
        repo = SQLiteEvaluationRepository(resolve_control_plane_database(ROOT))
        middleware = EvaluationMiddleware(repo, project_root=ROOT)
        schema = make_parameter_schema(parameters=[{"name": "x", "type": "float", "role": "variable", "bounds": {"min": 0.0, "max": 1.0}}], problem_hint="minimal", source_package=identity)
        schema_rec = middleware.register_schema(schema)
        problem = make_problem_definition(problem_id="problem:minimal", parameter_schema_revision=schema_rec["revision"], constraint_revision="sha256:" + "0" * 64, simulation_capabilities=["minimal-simulation"], metric_schema_revision="sha256:" + "1" * 64)
        middleware.register_problem(problem)
        candidate = make_candidate(problem_id=problem["problem_id"], problem_revision=problem["revision"], parameters={"x": 0.5})
        request = make_evaluation_request(candidate_id=candidate["candidate_id"], fidelity="standard", requested_outputs=["status"], evidence_profile="minimal")
        evaluation = middleware.submit_evaluation(candidate, request)
        from control_plane.runtime.composition import compose_runtime
        from control_plane.runtime.loop import RuntimeLoop
        context = compose_runtime(ROOT)
        try:
            loop = RuntimeLoop(context.dispatcher, min_interval=0, max_interval=0)
            terminal_status = None
            for round_no in range(1, 6):
                loop.run(max_rounds=1)
                final = middleware.get_evaluation(evaluation["evaluation_id"])
                terminal_status = final.get("status", final.get("evaluation_status"))
                print(f"round {round_no}: evaluation status: {terminal_status}")
                if terminal_status == "qualified":
                    break
            if terminal_status != "qualified":
                reason = _unresolved_reason(evaluation["evaluation_id"])
                print(
                    f"qualification failure reason: {reason}"
                    if reason else
                    "evaluation did not qualify (no stored unresolved reason)"
                )
            print(f"evaluation terminal status: {terminal_status}")
        finally:
            context.close()
        return 0
    finally:
        (ROOT / "records" / "artifacts" / "package.minimal.input.json").unlink(missing_ok=True)
        shutil.rmtree(RUNTIME, ignore_errors=True)
        shutil.rmtree(DATA, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
