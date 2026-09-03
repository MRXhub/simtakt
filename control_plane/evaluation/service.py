"""Simulator-neutral coordination API for scientific evaluations."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence
from control_plane.core.ports import ControlStore
from control_plane.evaluation.project_ports import ProjectFileControlStore
from control_plane.core.evaluation_contracts import (
    ContractError,
    observation_from_report,
    validate_algorithm_event,
    validate_algorithm_result,
    validate_algorithm_run,
    validate_candidate,
    validate_evaluation_request,
    validate_problem_definition,
    validate_qualification_report,
)
from control_plane.data.sqlite_evaluation_repository import RepositoryError, SQLiteEvaluationRepository
from control_plane.evaluation.control_plane import resolve_control_plane_database
from control_plane.evaluation.execution_options import (
    ExecutionOptionError,
    validate_execution_preparation,
)
from control_plane.evaluation.compute_profile import (
    ComputeProfileError,
    validate_feedback_observation,
)
from control_plane.evaluation.parallel_efficiency_calibration import (
    ParallelEfficiencyCalibrationError,
    build_parallel_efficiency_calibration_requests,
    validate_parallel_efficiency_calibration_configuration,
    validate_parallel_efficiency_calibration_request,
)
from control_plane.evaluation.governed_preparation import GovernedExecutionPreparation
from control_plane.evaluation.automation_policy import resolve_automation_policy
from control_plane.evaluation.scheduling_policy import (
    GovernedSchedulingPolicy,
    SchedulingPolicyError,
    resolve_governed_scheduling_policy,
)
from control_plane.simulation.session_contracts import (
    normalize_artifact_id,
    validate_simulation_session_result,
)
from control_plane.simulation.adapter_catalog import AdapterCatalogError, resolve_adapter

_LOGGER = logging.getLogger(__name__)


class EvaluationMiddleware:
    """Coordinate durable requests without knowing an optimizer or simulator."""

    def __init__(
        self,
        repository: SQLiteEvaluationRepository,
        *,
        project_root: Path | str | None = None,
        control_store: ControlStore | None = None,
    ) -> None:
        self._repository = repository
        self._project_root = (
            None if project_root is None else Path(project_root).resolve()
        )
        self._control_store = control_store or ProjectFileControlStore()

    @classmethod
    def for_project(cls, project_root: Path | str) -> "EvaluationMiddleware":
        """Open the one execution control domain used by all new algorithms."""

        root = Path(project_root).resolve()
        return EvaluationMiddleware(
            SQLiteEvaluationRepository(
                resolve_control_plane_database(root)
            ),
            project_root=root,
        )

    def register_schema(self, document: Mapping[str, Any]) -> dict[str, Any]:
        """Register one ParameterSchema document idempotently."""
        return self._repository.register_schema_document(document)

    def get_schema(self, revision: str) -> dict[str, Any]:
        """Fetch one ParameterSchema document by its stable revision."""
        return self._repository.get_schema_document(revision)

    def list_schemas(self) -> list[dict[str, Any]]:
        """List all registered ParameterSchema documents."""
        return self._repository.list_schema_documents()

    def list_packages(self) -> list[dict[str, Any]]:
        """List registered/materialized input packages from workspace storage and registry."""
        if self._project_root is None:
            return []
        root = Path(self._project_root).resolve()
        packages: dict[str, dict[str, Any]] = {}

        # 1. Scan records/artifacts shards for active input-package records
        artifacts_dir = root / "records" / "artifacts"
        if artifacts_dir.is_dir():
            try:
                for entry in sorted(artifacts_dir.glob("*.json")):
                    try:
                        data = json.loads(entry.read_text(encoding="utf-8-sig"))
                        if not isinstance(data, dict):
                            continue
                        if data.get("schema_version") != 1 or data.get("record_kind") != "artifact-catalog-shard":
                            continue
                        art = data.get("artifact")
                        if not isinstance(art, dict) or art.get("kind") != "input-package" or art.get("status") != "active":
                            continue
                        art_id = str(art.get("artifact_id", ""))
                        if not art_id:
                            continue
                        latest_rev = str(art.get("latest_revision", ""))
                        primary_path = None
                        for rev_entry in art.get("revisions", []):
                            if isinstance(rev_entry, dict) and rev_entry.get("revision") == latest_rev:
                                for loc in rev_entry.get("locations", []):
                                    if isinstance(loc, dict) and loc.get("role") == "primary":
                                        primary_path = loc.get("path")
                                        break
                                if primary_path:
                                    break

                        pkg_name = art_id.removeprefix("package.").removeprefix("pkg:")
                        if primary_path:
                            raw_pkg_path = root / primary_path
                        else:
                            raw_pkg_path = root / "data" / "inputs" / "packages" / pkg_name

                        try:
                            resolved_pkg_dir = raw_pkg_path.resolve()
                            rel_path = str(resolved_pkg_dir.relative_to(root)).replace("\\", "/")
                        except (ValueError, RuntimeError):
                            continue

                        pkg_item: dict[str, Any] = {
                            "package_name": pkg_name,
                            "artifact_id": art_id,
                            "revision": latest_rev,
                            "status": "active",
                            "path": rel_path,
                            "deck_file": "deck.in",
                            "created_at": art.get("status_changed_at"),
                            "dependencies": [],
                            "files": [],
                        }
                        manifest_file = resolved_pkg_dir / "manifest.json"
                        if manifest_file.is_file():
                            try:
                                m = json.loads(manifest_file.read_text(encoding="utf-8"))
                                if isinstance(m, dict):
                                    pkg_item["package_name"] = m.get("package_name", pkg_name)
                                    pkg_item["deck_file"] = m.get("deck_file", "deck.in")
                                    pkg_item["dependencies"] = m.get("dependencies", [])
                                    pkg_item["files"] = m.get("files", [])
                                    if m.get("created_at"):
                                        pkg_item["created_at"] = m["created_at"]
                            except Exception:
                                pass
                        packages[art_id] = pkg_item
                    except Exception:
                        continue
            except OSError:
                pass

        # 2. Scan data/inputs/packages directory for landed packages
        pkg_root = root / "data" / "inputs" / "packages"
        if pkg_root.is_dir():
            try:
                for entry in sorted(pkg_root.iterdir()):
                    if not entry.is_dir() or entry.name.startswith("."):
                        continue
                    try:
                        resolved_entry = entry.resolve()
                        rel_path = str(resolved_entry.relative_to(root)).replace("\\", "/")
                    except (ValueError, RuntimeError):
                        continue

                    manifest_file = resolved_entry / "manifest.json"
                    if not manifest_file.is_file():
                        continue
                    try:
                        m = json.loads(manifest_file.read_text(encoding="utf-8"))
                        if not isinstance(m, dict):
                            continue
                        pkg_name = m.get("package_name", entry.name)
                        art_id = m.get("artifact_id", f"pkg:{pkg_name}")
                        deck_file = m.get("deck_file", "deck.in")
                        deck_path = resolved_entry / deck_file
                        if deck_path.is_file():
                            rev = "sha256:" + hashlib.sha256(deck_path.read_bytes()).hexdigest().lower()
                        else:
                            rev = "sha256:" + hashlib.sha256(manifest_file.read_bytes()).hexdigest().lower()
                        pkg_item = {
                            "package_name": pkg_name,
                            "artifact_id": art_id,
                            "revision": rev,
                            "status": "registered",
                            "path": rel_path,
                            "deck_file": deck_file,
                            "created_at": m.get("created_at"),
                            "dependencies": m.get("dependencies", []),
                            "files": m.get("files", []),
                        }
                        packages[art_id] = pkg_item
                    except Exception:
                        continue
            except OSError:
                pass

        return list(packages.values())

    def list_problems(self) -> list[dict[str, Any]]:
        """List all registered ProblemDefinition records."""
        return self._repository.list_problems()

    def register_problem(self, definition: Mapping[str, Any]) -> dict[str, Any]:
        return self._repository.register_problem(validate_problem_definition(definition))

    def set_problem_status(self, problem_id: str, revision: str, status: str) -> dict[str, Any]:
        return self._repository.set_problem_status(problem_id, revision, status)

    def create_study(
        self,
        *,
        study_id: str,
        problem_id: str,
        problem_revision: str,
        metadata: Mapping[str, Any] | None = None,
        algorithm_run_id: str | None = None,
        artifact_refs: Sequence[Mapping[str, Any]] = (),
        automation_profile: str = "assisted",
    ) -> dict[str, Any]:
        return self._repository.create_study(
            study_id=study_id,
            problem_id=problem_id,
            problem_revision=problem_revision,
            metadata=metadata,
            algorithm_run_id=algorithm_run_id,
            artifact_refs=artifact_refs,
            automation_profile=automation_profile,
        )

    def get_study_status(self, study_id: str) -> dict[str, Any]:
        return self._repository.get_study_status(study_id)

    def list_studies(self, problem_id: str | None = None) -> list[dict[str, Any]]:
        return self._repository.list_studies(problem_id)

    def study_overviews(self, limit: int | None = None) -> dict[str, Any]:
        return self._repository.list_study_overviews(limit)

    def list_problem_evaluations(
        self, problem_id: str, problem_revision: str | None = None
    ) -> list[dict[str, Any]]:
        return self._repository.list_problem_evaluations(problem_id, problem_revision)

    def list_evaluations(
        self,
        problem_id: str | None = None,
        problem_revision: str | None = None,
        *,
        origin: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._repository.list_evaluations(
            problem_id, problem_revision, origin=origin
        )

    def submit(
        self,
        candidate: Mapping[str, Any],
        request: Mapping[str, Any],
        *,
        study_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_candidate = validate_candidate(candidate)
        normalized_request = validate_evaluation_request(request)
        self._assert_project_calibration_admission(
            normalized_candidate, normalized_request
        )
        return self._repository.submit_evaluation(
            normalized_candidate, normalized_request, study_id=study_id
        )

    def submit_evaluation(
        self,
        candidate: Mapping[str, Any],
        request: Mapping[str, Any],
        *,
        study_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit one Evaluation, optionally recording its Study membership."""

        return self.submit(candidate, request, study_id=study_id)

    def _assert_project_calibration_admission(
        self, candidate: Mapping[str, Any], request: Mapping[str, Any]
    ) -> None:
        """Require qualified predecessors for a governed calibration request.

        The task-specific CLI is the usual client, but this closes the same
        ordering invariant at the project-wide middleware boundary as well.
        Non-calibration clients retain their existing simulator-neutral path.
        """
        if self._project_root is None:
            return
        try:
            state = self._control_store.read_project_state(self._project_root)
        except (OSError, ValueError) as exc:
            raise ContractError(
                "PROJECT_STATE is unreadable during admission"
            ) from exc
        tasks = state.get("active_tasks") if isinstance(state, Mapping) else None
        if not isinstance(tasks, list):
            return
        matching_configurations: list[tuple[dict[str, Any], str]] = []
        for task in tasks:
            if (
                not isinstance(task, Mapping)
                or task.get("kind") != "simulation"
                or task.get("status") != "approved-prepared-execution"
                or task.get("problem_id") != candidate["problem_id"]
                or task.get("problem_revision") != candidate["problem_revision"]
                or task.get("parallel_efficiency_calibration") is None
            ):
                continue
            try:
                configuration = validate_parallel_efficiency_calibration_configuration(
                    task["parallel_efficiency_calibration"],
                    candidate_id=candidate["candidate_id"],
                )
            except ParallelEfficiencyCalibrationError as exc:
                raise ContractError(
                    "project parallel-efficiency calibration configuration is invalid"
                ) from exc
            if request["evidence_profile"] == configuration["evidence_profile"]:
                task_id = task.get("id")
                if not isinstance(task_id, str):
                    raise ContractError("calibration task_id is invalid")
                matching_configurations.append((configuration, task_id))
        if not matching_configurations:
            return
        if len(matching_configurations) != 1:
            raise ContractError(
                "EvaluationRequest matches more than one calibration task"
            )
        configuration, task_id = matching_configurations[0]
        try:
            replicate = validate_parallel_efficiency_calibration_request(
                candidate,
                task_id=task_id,
                configuration=configuration,
                request=request,
            )
        except ParallelEfficiencyCalibrationError as exc:
            raise ContractError(
                "EvaluationRequest is not an exact project calibration replicate"
            ) from exc

        try:
            existing = self._repository.get_evaluation(request["evaluation_id"])
        except RepositoryError as exc:
            if str(exc) != f"unknown Evaluation: {request['evaluation_id']}":
                raise
        else:
            try:
                validate_parallel_efficiency_calibration_request(
                    candidate,
                    task_id=task_id,
                    configuration=configuration,
                    request=existing,
                )
            except ParallelEfficiencyCalibrationError as exc:
                raise ContractError(
                    "stored calibration EvaluationRequest conflicts with its task"
                ) from exc
            return

        requests = build_parallel_efficiency_calibration_requests(
            candidate,
            task_id=task_id,
            configuration=configuration,
        )
        for predecessor in requests[: replicate["replicate_ordinal"] - 1]:
            predecessor_request = predecessor["request"]
            try:
                evaluation = self._repository.get_evaluation(
                    predecessor_request["evaluation_id"]
                )
            except RepositoryError as exc:
                if str(exc) != (
                    f"unknown Evaluation: {predecessor_request['evaluation_id']}"
                ):
                    raise
                raise ContractError(
                    "parallel-efficiency calibration predecessor is not qualified"
                ) from exc
            if evaluation.get("status") != "qualified":
                raise ContractError(
                    "parallel-efficiency calibration predecessor is not qualified"
                )

    def resolve_evaluations(
        self, submissions: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Submit requests and return their current qualified outputs, if any."""

        normalized = []
        for submission in submissions:
            if not isinstance(submission, Mapping) or set(submission) != {
                "candidate",
                "request",
            }:
                raise ContractError(
                    "each submission must contain candidate and request"
                )
            candidate = validate_candidate(submission["candidate"])
            request = validate_evaluation_request(submission["request"])
            if request["candidate_id"] != candidate["candidate_id"]:
                raise ContractError(
                    "EvaluationRequest references a different Candidate"
                )
            normalized.append((candidate, request))

        resolved = []
        for candidate, request in normalized:
            evaluation = self.submit(candidate, request)
            stored_request = validate_evaluation_request(
                {key: evaluation[key] for key in request}
            )
            sample = None
            if evaluation["status"] == "qualified":
                observation_id = evaluation["observation_id"]
                if not isinstance(observation_id, str):
                    raise RepositoryError(
                        "qualified Evaluation lacks an Observation"
                    )
                sample = self._repository.get_qualified_sample(observation_id)
                if sample["candidate"] != candidate:
                    raise RepositoryError(
                        "qualified Evaluation resolved a different Candidate"
                    )
            elif evaluation["observation_id"] is not None:
                raise RepositoryError(
                    "non-qualified Evaluation references an Observation"
                )
            resolved.append(
                {
                    "candidate": candidate,
                    "request": stored_request,
                    "evaluation": evaluation,
                    "sample": sample,
                }
            )
        return resolved

    def prepared_scheduling_candidates(
        self, limit: int | None = None
    ) -> list[dict[str, Any]]:
        return self._repository.list_prepared_scheduling_candidates(limit)

    def preparation_admission_snapshot(
        self, *, now: datetime | None = None
    ) -> dict[str, Any]:
        """Return only the queue fields needed by the project controller."""

        queued = self._repository.list_queued_evaluations(limit=None, now=now)
        return {
            "active_preparation_count": (
                self._repository.preparation_window_occupancy(now=now)
            ),
            "queued": [
                {
                    "evaluation_id": item["evaluation_id"],
                    "priority": item["priority"],
                    "queued_at": item["queued_since"],
                }
                for item in queued
            ],
        }

    def _scheduling_policy_for_provenance(
        self, policy_provenance: Mapping[str, Any]
    ) -> GovernedSchedulingPolicy:
        if self._project_root is None:
            raise ContractError(
                "Preparation admission requires the project control plane"
            )
        try:
            policy = resolve_governed_scheduling_policy(self._project_root)
        except SchedulingPolicyError as exc:
            raise ContractError("project SchedulingPolicy is unavailable") from exc
        if (
            not isinstance(policy_provenance, Mapping)
            or dict(policy_provenance) != policy.provenance()
        ):
            raise ContractError("SchedulingPolicy provenance changed")
        return policy

    def claim_preparation_slots(
        self,
        ordered_evaluation_ids: Sequence[str],
        *,
        controller_id: str,
        window_limit: int,
        lease_seconds: int,
        policy_provenance: Mapping[str, Any],
        now: datetime | None = None,
    ) -> list[dict[str, str]]:
        """Atomically claim the governed finite-window deficit."""

        policy = self._scheduling_policy_for_provenance(policy_provenance)
        if window_limit != policy.window_limit:
            raise ContractError("Preparation window differs from SchedulingPolicy")
        if lease_seconds != policy.preparation_claim_seconds:
            raise ContractError("Preparation lease differs from SchedulingPolicy")
        claims = self._repository.claim_preparation_slots(
            ordered_evaluation_ids,
            controller_id=controller_id,
            window_limit=window_limit,
            lease_seconds=lease_seconds,
            now=now,
        )
        return [
            {
                "claim_id": item["claim_id"],
                "evaluation_id": item["evaluation_id"],
            }
            for item in claims
        ]

    def commit_preparation_claim(
        self,
        claim_id: str,
        *,
        controller_id: str,
        preparation: GovernedExecutionPreparation,
        policy_provenance: Mapping[str, Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Commit only a project-attested Preparation under the current policy."""

        policy = self._scheduling_policy_for_provenance(policy_provenance)
        if (
            not isinstance(preparation, GovernedExecutionPreparation)
            or self._project_root is None
            or not preparation.is_attested_for(self._project_root)
            or preparation.provenance() != policy.provenance()
        ):
            raise ContractError(
                "execution preparation must come from project authority"
            )
        try:
            normalized = validate_execution_preparation(preparation.as_mapping())
        except ExecutionOptionError as exc:
            raise ContractError("execution preparation is invalid") from exc
        return self._repository.commit_preparation_claim(
            claim_id,
            controller_id,
            normalized,
            governance_provenance=preparation.provenance(),
            now=now,
        )

    def release_preparation_claim(
        self,
        claim_id: str,
        *,
        controller_id: str,
        reason: str,
        now: datetime | None = None,
    ) -> bool:
        return self._repository.release_preparation_claim(
            claim_id,
            controller_id,
            reason=reason,
            now=now,
        )

    def retire_unstarted_preparation(
        self,
        attempt_id: str,
        preparation_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self._repository.retire_unstarted_preparation(
            attempt_id,
            preparation_id,
            reason=reason,
            now=now,
        )

    def active_allocations(self, target_id: str | None = None) -> list[dict[str, Any]]:
        """List active allocations for one target, or all targets when omitted."""
        return self._repository.list_active_allocations(target_id)

    def capacity_counts(self) -> dict[str, int]:
        return self._repository.capacity_counts()

    def task_shape_statistics(self) -> list[dict[str, Any]]:
        return self._repository.list_task_shape_statistics()

    def budget_proposals(self, *, tier2_min_samples: int = 30) -> list[dict[str, Any]]:
        return self._repository.budget_proposals(tier2_min_samples=tier2_min_samples)

    def stale_reconciling_attempts(
        self,
        stale_seconds: int = 3600,
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Read-only SLA discovery for reconciling Attempts (one-hour threshold by default)."""
        return self._repository.list_stale_reconciling_attempts(
            stale_seconds, now=now
        )

    def has_reconciling_attempts_for_wall_proof(self) -> bool:
        return self._repository.has_reconciling_attempts_for_wall_proof()


    def auto_release_wall_budget(
        self, *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Compute each Attempt's proof from its persisted immutable budget."""
        candidates = self._repository.list_reconciling_attempts_for_wall_proof()
        if not candidates:
            return []
        multiplier = 1.7
        reconcile_hold_seconds = None
        if self._project_root is not None:
            try:
                governed_policy = resolve_governed_scheduling_policy(self._project_root)
                multiplier = governed_policy.kill_multiplier
                reconcile_hold_seconds = governed_policy.reconcile_hold_seconds
            except SchedulingPolicyError:
                pass
        proofs: dict[str, int] = {}
        unreadable_budget_ids: set[str] = set()
        for candidate in candidates:
            budget: Mapping[str, Any] | None = None
            for plan_key in ("execution_preparation", "execution_plan"):
                plan = candidate.get(plan_key)
                if isinstance(plan, Mapping) and isinstance(plan.get("budget"), Mapping):
                    budget = plan["budget"]
                    break
            persisted_budget = candidate.get("wall_budget")
            if persisted_budget is not None:
                budget = persisted_budget
            if (
                isinstance(budget, Mapping)
                and isinstance(budget.get("kill_at_seconds"), int)
                and not isinstance(budget.get("kill_at_seconds"), bool)
            ):
                proofs[candidate["attempt_id"]] = int(budget["kill_at_seconds"])
                continue
            max_wall = budget.get("max_wall_seconds") if isinstance(budget, Mapping) else None
            command_timeout = budget.get("command_timeout_seconds") if isinstance(budget, Mapping) else None
            if (
                isinstance(max_wall, bool)
                or not isinstance(max_wall, int)
                or max_wall < 1
                or isinstance(command_timeout, bool)
                or not isinstance(command_timeout, int)
                or command_timeout < 1
            ):
                unreadable_budget_ids.add(candidate["attempt_id"])
            else:
                proofs[candidate["attempt_id"]] = int(multiplier * max_wall)
        results = self._repository.auto_release_wall_budget(
            proofs, now=now, reconcile_hold_seconds=reconcile_hold_seconds
        )
        for result in results:
            if (
                result.get("attempt_id") in unreadable_budget_ids
                and result.get("status") == "skipped"
            ):
                result["reason"] = "budget-unavailable"
                result["budget_status"] = "unreadable"
        return results

    def has_recovering_evaluations(self) -> bool:
        return self._repository.has_recovering_evaluations()

    def auto_requeue_recovering(
        self, *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        policy = resolve_automation_policy(self._project_root) if self._project_root is not None else None
        return self._repository.auto_requeue_recovering(now=now, automation_policy=policy)

    def operator_requeue(
        self,
        evaluation_id: str,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self._repository.operator_requeue(evaluation_id, reason, now=now)

    def force_lost_attempt(
        self,
        attempt_id: str,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Perform the operator-only, explicitly reasoned lost transition."""
        return self._repository.force_lost_attempt(attempt_id, reason, now=now)

    def claim_prepared_execution(
        self,
        attempt_id: str,
        dispatcher_id: str,
        lease_seconds: int,
        *,
        preparation_id: str,
        selected_option_id: str,
        session_plan: Mapping[str, Any],
        allocation: Mapping[str, Any],
        license_sessions: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        return self._repository.claim_prepared_execution(
            attempt_id,
            dispatcher_id,
            lease_seconds,
            preparation_id=preparation_id,
            selected_option_id=selected_option_id,
            session_plan=session_plan,
            allocation=allocation,
            license_sessions=license_sessions,
            now=now,
        )

    def confirm_attempt_start(
        self,
        attempt_id: str,
        dispatcher_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self._repository.confirm_attempt_start(
            attempt_id, dispatcher_id, now=now
        )

    def lease_next_reconciliation(
        self,
        observer_id: str,
        lease_seconds: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        return self._repository.lease_next_reconciliation(
            observer_id, lease_seconds, now=now
        )

    def has_reconciliation_candidate(self) -> bool:
        return self._repository.has_reconciliation_candidate()


    def has_pending_terminations(self) -> bool:
        return self._repository.has_pending_terminations()

    def get_next_pending_termination(self) -> dict[str, Any] | None:
        return self._repository.get_next_pending_termination()

    def update_termination_state(
        self, attempt_id: str, termination_state: str, *, now: datetime | None = None
    ) -> dict[str, Any]:
        return self._repository.update_termination_state(
            attempt_id, termination_state, now=now
        )
    def heartbeat(
        self,
        attempt_id: str,
        worker_id: str,
        lease_seconds: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self._repository.heartbeat(
            attempt_id, worker_id, lease_seconds, now=now
        )

    def begin_collection(
        self, attempt_id: str, worker_id: str, *, now: datetime | None = None
    ) -> dict[str, Any]:
        return self._repository.begin_collection(attempt_id, worker_id, now=now)

    def fail_attempt(
        self,
        attempt_id: str,
        worker_id: str,
        failure_class: str,
        artifact_ids: Sequence[str] = (),
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self._repository.fail_attempt(
            attempt_id,
            worker_id,
            failure_class,
            artifact_ids,
            now=now,
        )

    def complete_session(
        self,
        result: Mapping[str, Any],
        worker_id: str,
        result_artifact_id: str,
        *,
        feedback: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized = validate_simulation_session_result(result)
        attempt = self._repository.get_attempt(normalized["attempt_id"])
        if (
            attempt["execution_plan_id"] != normalized["plan_id"]
            or attempt["session_ref"] != normalized["session_ref"]
        ):
            raise RepositoryError("session result does not match the bound Attempt")
        artifacts = sorted(
            {
                normalize_artifact_id(result_artifact_id, "result_artifact_id"),
                normalized["journal_artifact_id"],
                *normalized["evidence_artifact_ids"],
            }
        )
        if normalized["status"] == "completed":
            observation = self._terminal_feedback(
                feedback=feedback, success=True, solver_run_records=normalized.get("solver_run_records")
            )
            completed = self._repository.complete_attempt(
                normalized["attempt_id"],
                worker_id,
                artifacts,
                now=now,
                _validated_session_result=True,
                feedback=observation,
            )
            self._qualify_completed_attempt(
                normalized["attempt_id"], tuple(artifacts), completed
            )
            return completed
        if normalized["status"] == "exhausted":
            observation = self._terminal_feedback(
                feedback=feedback, success=False, solver_run_records=normalized.get("solver_run_records")
            )
            return self._repository.fail_attempt(
                normalized["attempt_id"],
                worker_id,
                "recovery-exhausted",
                artifacts,
                now=now,
                feedback=observation,
            )
        if feedback is not None:
            raise ContractError("indeterminate session cannot record completion feedback")
        return self._repository.mark_attempt_reconciling(
            normalized["attempt_id"],
            worker_id,
            artifacts,
            reason="proxy-session-indeterminate",
            now=now,
        )

    def _qualify_completed_attempt(
        self,
        attempt_id: str,
        collected_artifact_ids: Sequence[str],
        completed: Mapping[str, Any],
    ) -> None:
        """Qualify a completed attempt, marking its Evaluation unresolved on failure."""
        if not isinstance(completed, Mapping):
            return
        if self._project_root is None:
            raise AdapterCatalogError("adapter root is not configured")
        attempt = self._repository.get_attempt(attempt_id)
        evaluation = self._repository.get_evaluation(attempt["evaluation_id"])
        # Qualification is driven by the Evaluation, which `complete_attempt`
        # transitions to ``qualifying``; the returned Attempt record itself is
        # ``completed`` and is not a signal to skip qualification.
        if evaluation.get("status") != "qualifying":
            return
        try:
            evaluation_input = self._repository.get_evaluation_input(attempt["evaluation_id"])
            candidate = evaluation_input["candidate"]
            attempts = self._repository.list_evaluation_attempts(attempt["evaluation_id"])
            artifact_ids = tuple(sorted({
                artifact_id
                for item in attempts
                for artifact_id in item.get("artifact_ids", ())
                if isinstance(artifact_id, str)
            } | set(collected_artifact_ids)))
            context = MappingProxyType({
                "evaluation_id": str(attempt["evaluation_id"]),
                "candidate_id": str(candidate["candidate_id"]),
                "attempt_ids": tuple(str(item["attempt_id"]) for item in attempts),
                "artifact_ids": artifact_ids,
            })
            resolved = resolve_adapter(self._project_root, str(attempt["simulation_adapter"]))
            report = resolved.adapter.qualify(self, attempt_id, context)
            if not isinstance(report, Mapping):
                raise ContractError("adapter qualification report must be an object")
            self.record_qualification(report)
        except RepositoryError:
            raise
        except Exception as exc:
            reason = f"qualification-failed: {exc}"
            _LOGGER.warning(
                "adapter qualification failed for attempt %s: %s",
                attempt_id, exc, exc_info=True
            )
            attempt = self._repository.get_attempt(attempt_id)
            self.mark_unresolved(str(attempt["evaluation_id"]), reason)


    def _terminal_feedback(
        self,
        *,
        success: bool,
        feedback: Mapping[str, Any] | None,
        solver_run_records: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        if feedback is None:
            return None
        if not isinstance(feedback, Mapping):
            raise ContractError("completion feedback is invalid")
        derived: dict[str, Any] = {}
        records = solver_run_records or ()
        for field, record_field, reducer in (
            ("wall_seconds", "wall_seconds", sum),
            ("cpu_seconds", "cpu_seconds", sum),
            ("rss_bytes", "peak_rss_bytes", max),
        ):
            values = [record[record_field] for record in records if record.get(record_field) is not None]
            if values:
                derived[field] = reducer(values)
        try:
            return validate_feedback_observation(
                {
                    "success": success,
                    "wall_seconds": feedback["wall_seconds"]
                    if "wall_seconds" in feedback
                    else derived.get("wall_seconds"),
                    "cpu_seconds": feedback["cpu_seconds"]
                    if "cpu_seconds" in feedback
                    else derived.get("cpu_seconds"),
                    "busy_seconds": feedback.get("busy_seconds"),
                    "rss_bytes": feedback["rss_bytes"]
                    if "rss_bytes" in feedback
                    else derived.get("rss_bytes"),
                }
            )
        except ComputeProfileError as exc:
            raise ContractError("completion feedback is invalid") from exc

    def record_attempt_feedback(
        self,
        attempt_id: str,
        *,
        success: bool,
        wall_seconds: float | None = None,
        cpu_seconds: float | None = None,
        busy_seconds: float | None = None,
        rss_bytes: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Submit one typed terminal feedback observation."""
        try:
            observation = validate_feedback_observation(
                {
                    "success": success,
                    "wall_seconds": wall_seconds,
                    "cpu_seconds": cpu_seconds,
                    "busy_seconds": busy_seconds,
                    "rss_bytes": rss_bytes,
                }
            )
        except ComputeProfileError as exc:
            raise ContractError("attempt feedback is invalid") from exc
        return self._repository.record_attempt_feedback(
            attempt_id, now=now, **observation
        )

    def record_attempt_auto_feedback(self, attempt_id: str) -> None:
        """Best-effort, idempotent feedback for one terminal Attempt.

        Builds the feedback observation from durable timestamps
        (``updated_at`` - ``created_at``) and folds it into the task-class
        shape statistics.  Success is derived from the terminal status
        (``completed`` succeeds; ``failed``/``lost`` do not).  Idempotent via
        the ``attempt_feedback`` primary key and the Attempt's
        ``feedback_json`` marker, and any failure is swallowed because
        statistics are an accessory to — never a fact of — task termination.
        """
        attempt = self._repository.get_attempt(attempt_id)
        status = str(attempt["status"])
        if status == "completed":
            success = True
        elif status in {"failed", "lost"}:
            success = False
        else:
            raise ContractError(
                "Attempt feedback requires a terminal Attempt"
            )
        self._repository._record_auto_feedback(
            attempt_id,
            success=success,
            terminal_time=attempt["updated_at"],
        )

    def capacity_profile_snapshot(
        self, relevant_identities: Sequence[Mapping[str, Any]] = ()
    ) -> dict[str, Any]:
        """Return the bounded read-only capacity profile snapshot."""
        return self._repository.get_capacity_profile_snapshot(relevant_identities)

    def has_expired_leases(self, *, now: datetime | None = None) -> bool:
        return self._repository.has_expired_leases(now=now)

    def expire_leases(self, *, now: datetime | None = None) -> list[str]:
        return self._repository.expire_leases(now=now)

    def require_reconciliation(
        self,
        attempt_id: str,
        worker_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self._repository.mark_attempt_reconciling(
            attempt_id, worker_id, reason=reason, now=now
        )

    def reconcile_attempt(
        self,
        attempt_id: str,
        observer_id: str,
        session_ref: str,
        observed_status: str,
        lease_seconds: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self._repository.reconcile_attempt(
            attempt_id,
            observer_id,
            session_ref,
            observed_status,
            lease_seconds,
            now=now,
        )

    def plan_recovery(
        self,
        evaluation_id: str,
        reason: str,
        *,
        source: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        return self._repository.plan_recovery(
            evaluation_id, reason, source=source, now=now
        )

    def mark_unresolved(self, evaluation_id: str, reason: str) -> dict[str, Any]:
        return self._repository.mark_unresolved(evaluation_id, reason)

    def record_qualification(self, report: Mapping[str, Any]) -> dict[str, Any]:
        normalized = validate_qualification_report(report)
        observation = (
            observation_from_report(normalized)
            if normalized["status"] == "qualified"
            else None
        )
        return self._repository.record_qualification(normalized, observation)

    def get_evaluation(self, evaluation_id: str) -> dict[str, Any]:
        return self._repository.get_evaluation(evaluation_id)

    def get_evaluation_input(self, evaluation_id: str) -> dict[str, Any]:
        return self._repository.get_evaluation_input(evaluation_id)

    def get_attempt(self, attempt_id: str) -> dict[str, Any]:
        return self._repository.get_attempt(attempt_id)

    def list_evaluation_attempts(
        self, evaluation_id: str
    ) -> list[dict[str, Any]]:
        return self._repository.list_evaluation_attempts(evaluation_id)

    def get_observation(self, observation_id: str) -> dict[str, Any]:
        return self._repository.get_observation(observation_id)

    def get_qualified_sample(self, observation_id: str) -> dict[str, Any]:
        return self._repository.get_qualified_sample(observation_id)

    def register_algorithm_run(
        self, run: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._repository.register_algorithm_run(validate_algorithm_run(run))

    def record_algorithm_records(
        self, records: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Persist one retry-safe AlgorithmRun/Event/Result projection."""

        if not isinstance(records, Mapping) or set(records) != {
            "run",
            "events",
            "result",
        }:
            raise ContractError(
                "algorithm records must contain run, events, and result"
            )
        run = validate_algorithm_run(records["run"])
        raw_events = records["events"]
        if not isinstance(raw_events, Sequence) or isinstance(
            raw_events, (str, bytes, bytearray)
        ):
            raise ContractError("algorithm events must be a sequence")
        events = [validate_algorithm_event(event) for event in raw_events]
        result = (
            None
            if records["result"] is None
            else validate_algorithm_result(records["result"])
        )
        run_id = run["algorithm_run_id"]
        if any(event["algorithm_run_id"] != run_id for event in events):
            raise ContractError("AlgorithmEvent references a different AlgorithmRun")
        if result is not None and (
            result["algorithm_run_id"] != run_id
            or result["algorithm_id"] != run["algorithm_id"]
            or result["algorithm_revision"] != run["algorithm_revision"]
            or result["problem_id"] != run["problem_id"]
            or result["problem_revision"] != run["problem_revision"]
        ):
            raise ContractError("AlgorithmResult lineage differs from AlgorithmRun")

        stored_run = self._repository.register_algorithm_run(run)
        stored_events = [
            self._repository.record_algorithm_event(event) for event in events
        ]
        stored_result = (
            None
            if result is None
            else self._repository.record_algorithm_result(result)
        )
        return {
            "run": self._repository.get_algorithm_run(
                stored_run["algorithm_run_id"]
            ),
            "events": stored_events,
            "result": stored_result,
        }

    def list_algorithm_runs(self) -> list[dict[str, Any]]:
        return self._repository.list_algorithm_runs()

    def get_algorithm_run(self, algorithm_run_id: str) -> dict[str, Any]:
        return self._repository.get_algorithm_run(algorithm_run_id)

    def record_algorithm_event(
        self, event: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._repository.record_algorithm_event(validate_algorithm_event(event))

    def list_algorithm_events(
        self, algorithm_run_id: str
    ) -> list[dict[str, Any]]:
        return self._repository.list_algorithm_events(algorithm_run_id)

    def record_algorithm_result(
        self, result: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._repository.record_algorithm_result(
            validate_algorithm_result(result)
        )

    def get_algorithm_result(self, algorithm_result_id: str) -> dict[str, Any]:
        return self._repository.get_algorithm_result(algorithm_result_id)

    def list_algorithm_results(
        self, algorithm_run_id: str
    ) -> list[dict[str, Any]]:
        return self._repository.list_algorithm_results(algorithm_run_id)

    def export_algorithm_run(self, algorithm_run_id: str) -> dict[str, Any]:
        return self._repository.export_algorithm_run(algorithm_run_id)

    def archive_algorithm_run(
        self,
        algorithm_run_id: str,
        *,
        bundle_revision: str,
        archive_artifact_id: str,
        archive_revision: str,
    ) -> dict[str, Any]:
        return self._repository.archive_algorithm_run(
            algorithm_run_id,
            bundle_revision=bundle_revision,
            archive_artifact_id=archive_artifact_id,
            archive_revision=archive_revision,
        )
