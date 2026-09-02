"""Materialize a minimal package, register it, and build a preparation."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent.parent))

from control_plane.core.evaluation_contracts import canonical_json
from control_plane.core.workspace_artifacts import resolve_workspace_artifact
from control_plane.evaluation.execution_options import (
    make_execution_option,
    make_execution_option_set,
    make_execution_preparation,
    make_performance_profile,
    make_performance_profile_snapshot,
)
from control_plane.simulation.adapter_catalog import resolve_adapter

ROOT = Path(__file__).resolve().parent
CANDIDATE_ID = "candidate:sha256:" + "1" * 64
PACKAGE_ID = "minimal-simulation.package"


def _register_package(package_dir: Path, artifact_id: str) -> dict[str, str]:
    manifest_path = package_dir / "manifest.json"
    revision = "sha256:" + hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    shard = {
        "schema_version": 1,
        "record_kind": "artifact-catalog-shard",
        "artifact": {
            "artifact_id": artifact_id,
            "kind": "input-package",
            "status": "active",
            "latest_revision": revision,
            "revisions": [{
                "revision": revision,
                "hash_scope": "package-manifest",
                "locations": [{
                    "storage": "workspace", "role": "primary", "availability": "current",
                    "path": str(package_dir.relative_to(ROOT)).replace("\\", "/"),
                }],
            }],
        },
    }
    (ROOT / "records" / "artifacts" / f"{artifact_id}.json").write_text(
        json.dumps(shard, indent=2) + "\n", encoding="utf-8"
    )
    return {"artifact_id": artifact_id, "revision": revision}


def main() -> int:
    shutil.rmtree(ROOT / ".runtime" / "materialized-package", ignore_errors=True)
    resolved = resolve_adapter(ROOT, "minimal-simulation")
    evaluation_input = {"voltage": 1.0, "temperature": 300}
    task = {"task_id": "minimal-materialize-task", "candidate_id": CANDIDATE_ID}
    materialized = resolved.adapter.materialize_package(evaluation_input, task)

    package_dir = ROOT / ".runtime" / "materialized-package"
    package_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(materialized["path"], package_dir / "input.pkg")
    manifest = {
        "schema_version": 1,
        "artifact_id": PACKAGE_ID,
        "design": {"candidate_id": CANDIDATE_ID},
        "execution": {"processors": 1},
        "files": [{"path": "input.pkg"}],
    }
    (package_dir / "manifest.json").write_text(
        canonical_json(manifest) + "\n", encoding="utf-8"
    )
    identity = _register_package(package_dir, PACKAGE_ID)
    checked = resolve_workspace_artifact(ROOT, PACKAGE_ID, expected_kind="input-package")

    option = make_execution_option(
        simulation_definition_artifact_id="simulation-definition.minimal",
        simulation_definition_revision="sha256:" + "2" * 64,
        runnable_package_artifact_id=identity["artifact_id"],
        runnable_package_revision=identity["revision"],
        target_id="local", processors=1, memory_bytes=1024**3,
        performance_class_id="performance-class:sha256:" + "3" * 64,
    )
    options = make_execution_option_set([option])
    profile = make_performance_profile(
        execution_option_id=option["option_id"],
        evidence_artifact_id="evidence.performance.minimal",
        evidence_revision="sha256:" + "4" * 64,
        sample_count=1, duration_p50_seconds=1, duration_p90_seconds=2,
        peak_rss_p90_bytes=512 * 1024**2,
        performance_class_id=option["performance_class_id"],
    )
    preparation = make_execution_preparation(
        evaluation_id="evaluation:minimal-materialize",
        candidate_id=CANDIDATE_ID, simulation_proxy="minimal-simulation",
        numerical_profile="minimal-v1", recovery_profile_revision="sha256:" + "5" * 64,
        task_id=task["task_id"], authorization_id="authorization.minimal",
        authorization_revision="sha256:" + "6" * 64,
        command_timeout_seconds=60, max_solver_runs=1, max_wall_seconds=60,
        execution_option_set=options,
        performance_profile_snapshot=make_performance_profile_snapshot(
            policy_revision="sha256:" + "7" * 64, profiles=[profile]
        ),
    )
    runnable = preparation["execution_option_set"]["options"][0]["runnable_package"]
    print(f"materialized artifact_id={checked.artifact_id} revision={checked.revision}")
    print(f"preparation runnable_package artifact_id={runnable['artifact_id']} revision={runnable['revision']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
