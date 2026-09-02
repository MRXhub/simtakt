"""Lazy queued Evaluation -> prepared Attempt phase.

The phase is intentionally a small orchestration boundary: durable queue and
claim semantics remain in the repository, while adapter/package policy lives
here.  It never reads PROJECT_STATE.json.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

from control_plane.core.workspace_artifacts import resolve_workspace_artifact
from control_plane.evaluation.execution_options import (
    make_execution_option, make_execution_option_set,
    make_performance_profile, make_performance_profile_snapshot,
    make_execution_preparation,
)
from control_plane.simulation.adapter_catalog import resolve_adapter_for_problem

_LOG = logging.getLogger(__name__)


class PreparationPhase:
    """Prepare at most ``queued`` versus available capacity plus lookahead."""

    def __init__(self, repository: Any, project_root: Path | str, *, lookahead: int = 1,
                 window_limit: int = 1, controller_id: str = "runtime") -> None:
        self.repository = repository
        self.project_root = Path(project_root).resolve()
        self.lookahead = max(0, int(lookahead))
        self.window_limit = max(1, int(window_limit))
        self.controller_id = controller_id

    def _make(self, item: Mapping[str, Any]) -> Mapping[str, Any]:
        eid = str(item["evaluation_id"])
        inp = self.repository.get_evaluation_input(eid)
        problem, candidate = inp["problem"], inp["candidate"]
        if problem.get("status") == "paused":
            raise ValueError("problem is paused")
        adapter = resolve_adapter_for_problem(self.project_root, problem)
        schema = self.repository.get_schema(problem["parameter_schema_revision"])
        canonical = schema.get("canonical_json", schema)
        package = canonical.get("source_package") if isinstance(canonical, Mapping) else None
        if not isinstance(package, Mapping):
            raise ValueError("schema source_package is missing")
        aid, rev = str(package.get("artifact_id")), str(package.get("revision"))
        resolve_workspace_artifact(self.project_root, aid, revision=rev, expected_kind="input-package")
        targets = [t for t in self.repository.target_catalog.read_targets(self.project_root)
                   if t.get("status") == "active"] if hasattr(self.repository, "target_catalog") else []
        target = next((t for t in targets if int(t.get("processors", 10**9)) >= int(adapter.entry["resource_defaults"]["processors"])), None)
        if target is None:
            target_id = str(adapter.entry.get("target_id", "default"))
        else:
            target_id = str(target["target_id"])
        resources = adapter.entry["resource_defaults"]
        definition = adapter.entry.get("simulation_definition", package)
        option = make_execution_option(simulation_definition_artifact_id=str(definition["artifact_id"]), simulation_definition_revision=str(definition["revision"]), runnable_package_artifact_id=aid, runnable_package_revision=rev, target_id=target_id, processors=int(resources["processors"]), memory_bytes=int(resources["memory_bytes"]), performance_class_id=str(adapter.entry.get("performance_class_id", "default")))
        options = make_execution_option_set([option])
        profile = make_performance_profile(execution_option_id=option["option_id"], evidence_artifact_id=aid, evidence_revision=rev, sample_count=1, median_seconds=1.0, p90_seconds=1.0, peak_rss_p90_bytes=int(resources["memory_bytes"]), performance_class_id=option["performance_class_id"])
        profiles = make_performance_profile_snapshot(policy_revision=str(adapter.entry.get("policy_revision", rev)), profiles=[profile])
        return make_execution_preparation(evaluation_id=eid, candidate_id=str(candidate["candidate_id"]), simulation_proxy=adapter.adapter_id, numerical_profile=str(adapter.entry.get("numerical_profile", "default")), recovery_profile_revision=str(adapter.entry.get("recovery_profile_revision", "sha256:" + "0"*64)), command_timeout_seconds=int(resources["max_wall_seconds"]), max_solver_runs=1, max_wall_seconds=int(resources["max_wall_seconds"]), execution_option_set=options, performance_profile_snapshot=profiles)

    def prepare_once(self) -> int:
        occupied = self.repository.preparation_window_occupancy()
        limit = max(0, self.window_limit + self.lookahead - occupied)
        if not limit:
            return 0
        items = self.repository.list_queued_evaluations(limit=None)
        items.sort(key=lambda x: (-str(x.get("priority", "normal")), str(x.get("queued_since", x.get("created_at", "")))))
        claimed = self.repository.claim_preparation_slots([str(x["evaluation_id"]) for x in items], window_limit=self.window_limit, controller_id=self.controller_id)
        count = 0
        for item in claimed[:limit]:
            try:
                prep = self._make(item)
                self.repository.commit_preparation_claim(item["claim_id"], self.controller_id, prep)
                count += 1
            except OSError as exc:
                _LOG.warning("preparation skipped for %s: %s", item.get("evaluation_id"), exc)
            except Exception as exc:
                self.repository.release_preparation_claim(item["claim_id"], self.controller_id)
                self.repository.mark_unresolved(str(item["evaluation_id"]), reason=f"preparation-failed: {exc}")
        return count
