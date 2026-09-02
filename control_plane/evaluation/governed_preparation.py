"""Resolve project-authorized execution choices without caller resource input."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from control_plane.core.ports import ControlStore, TargetCatalog
from control_plane.evaluation.execution_topology import ProjectFileTargetCatalog
from control_plane.evaluation.project_ports import ProjectFileControlStore
from control_plane.core.workspace_artifacts import (
    WorkspaceArtifactError,
    resolve_workspace_artifact,
)
from control_plane.evaluation.execution_options import (
    ExecutionOptionError,
    make_parallel_efficiency_calibration,
    validate_execution_preparation,
    validate_parallel_efficiency_calibration,
)
from control_plane.evaluation.parallel_efficiency_calibration import (
    ParallelEfficiencyCalibrationError,
    validate_parallel_efficiency_calibration_configuration,
)
from control_plane.evaluation.scheduling_policy import (
    GovernedSchedulingPolicy,
    SchedulingPolicyError,
    resolve_governed_scheduling_policy,
)


_EXECUTABLE_TASK_STATUS = "approved-prepared-execution"
_PREPARED_AUTHORIZATION_KIND = "prepared-execution-envelope-v1"
_LAUNCH_MARGIN_SECONDS = 30
_GOVERNANCE_SEAL = object()


class GovernedPreparationError(RuntimeError):
    """Raised when project state does not uniquely authorize a Preparation."""


class GovernedExecutionPreparation:
    """Opaque authority result accepted by the project middleware boundary."""

    __slots__ = (
        "_canonical_preparation",
        "_project_root",
        "_provenance",
        "_seal",
    )

    def __init__(
        self,
        preparation: Mapping[str, Any],
        *,
        project_root: Path,
        artifact_id: str,
        artifact_revision: str,
        project_state_revision: str,
        _seal: object,
    ) -> None:
        if _seal is not _GOVERNANCE_SEAL:
            raise GovernedPreparationError(
                "GovernedExecutionPreparation must come from project authority"
            )
        self._canonical_preparation = json.dumps(
            dict(preparation),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._project_root = Path(project_root).resolve()
        self._provenance = {
            "artifact_id": artifact_id,
            "revision": artifact_revision,
            "project_state_revision": project_state_revision,
        }
        self._seal = _seal

    def as_mapping(self) -> dict[str, Any]:
        return json.loads(self._canonical_preparation)

    def provenance(self) -> dict[str, str]:
        return dict(self._provenance)

    def is_attested_for(self, project_root: Path | str) -> bool:
        return (
            self._seal is _GOVERNANCE_SEAL
            and self._project_root == Path(project_root).resolve()
        )


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GovernedPreparationError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise GovernedPreparationError(f"{label} must be a JSON object")
    return value


def _read_json_with_revision(
    path: Path, label: str
) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GovernedPreparationError(f"cannot read {label}") from exc
    if not isinstance(value, dict):
        raise GovernedPreparationError(f"{label} must be a JSON object")
    return value, "sha256:" + hashlib.sha256(raw).hexdigest()


def _aware_time(value: Any, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise GovernedPreparationError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise GovernedPreparationError(f"{label} must include a timezone")
    return parsed


def _resolve_json_artifact(
    project_root: Path,
    artifact_id: str,
    revision: str,
    *,
    expected_kind: str,
    label: str,
) -> tuple[dict[str, Any], Path]:
    try:
        resolved = resolve_workspace_artifact(
            project_root,
            artifact_id,
            revision=revision,
            expected_kind=expected_kind,
        )
    except WorkspaceArtifactError as exc:
        raise GovernedPreparationError(
            f"{label} is not an exact active artifact"
        ) from exc
    if resolved.hash_scope != "file":
        raise GovernedPreparationError(f"{label} must use file hash scope")
    return _read_json(resolved.path, label), resolved.path
def _authorization(
    project_root: Path,
    task: Mapping[str, Any],
    preparation: Mapping[str, Any],
    targets: Mapping[str, Mapping[str, Any]],
    *,
    now: datetime,
    target_catalog: TargetCatalog | None = None,
) -> list[Mapping[str, Any]]:
    """Resolve every active single-target envelope recorded by a Preparation."""

    authorization = preparation["authorization"]
    lineage = preparation.get("authorizations")
    requested = lineage if lineage is not None else [authorization]
    if not isinstance(requested, list) or not requested:
        raise GovernedPreparationError(
            "preparation authorization lineage is invalid"
        )
    if lineage is not None and len(requested) < 2:
        raise GovernedPreparationError(
            "preparation authorization lineage is invalid"
        )
    references = task.get("execution_authorizations")
    if not isinstance(references, list):
        raise GovernedPreparationError(
            "prepared execution task lacks execution_authorizations"
        )
    active_references = [
        item
        for item in references
        if isinstance(item, Mapping)
        and item.get("status") == "active"
        and item.get("authorization_kind") == _PREPARED_AUTHORIZATION_KIND
    ]
    requested_identities = {
        (item.get("artifact_id"), str(item.get("revision", "")).lower())
        for item in requested
        if isinstance(item, Mapping)
    }
    if len(requested_identities) != len(requested):
        raise GovernedPreparationError(
            "preparation authorization lineage contains duplicates"
        )
    if len(requested) > 1 and any(
        not isinstance(item, Mapping)
        or not str(item.get("target_id", "")).strip()
        for item in requested
    ):
        raise GovernedPreparationError(
            "each preparation authorization must identify one target"
        )

    active_identities = {
        (item.get("artifact_id"), str(item.get("revision", "")).lower())
        for item in active_references
    }
    if (
        len(active_identities) != len(active_references)
        or requested_identities != active_identities
    ):
        raise GovernedPreparationError(
            "preparation authorization lineage must cover every active authorization"
        )
    active_targets = [str(item.get("target_id", "")).strip() for item in active_references]
    if any(not target for target in active_targets) or len(active_targets) != len(set(active_targets)):
        raise GovernedPreparationError(
            "active execution authorizations must identify unique targets"
        )

    options = preparation["execution_option_set"]["options"]
    option_targets = {option["target_id"] for option in options}
    records: list[Mapping[str, Any]] = []
    seen_targets: set[str] = set()
    budget = preparation["budget"]
    catalog = target_catalog or ProjectFileTargetCatalog()
    try:
        _, targets_revision = catalog.read_targets_with_revision(project_root)
    except (OSError, ValueError) as exc:
        raise GovernedPreparationError("cannot hash execution targets") from exc
    calibration = _parallel_efficiency_calibration(task, preparation)

    for requested_item in requested:
        if not isinstance(requested_item, Mapping):
            raise GovernedPreparationError(
                "preparation authorization lineage is invalid"
            )
        artifact_id = requested_item.get("artifact_id")
        revision = str(requested_item.get("revision", "")).lower()
        matches = [
            item
            for item in active_references
            if item.get("artifact_id") == artifact_id
            and str(item.get("revision", "")).lower() == revision
        ]
        if len(matches) != 1:
            raise GovernedPreparationError(
                "preparation authorization is not uniquely active for its task"
            )
        reference = matches[0]
        reference_target = str(reference.get("target_id", "")).strip()
        if not reference_target or reference_target in seen_targets:
            raise GovernedPreparationError(
                "active execution authorizations must identify unique targets"
            )
        seen_targets.add(reference_target)
        if requested_item.get("target_id") not in (None, reference_target):
            raise GovernedPreparationError(
                "preparation authorization target conflicts with PROJECT_STATE"
            )
        body, _ = _resolve_json_artifact(
            project_root,
            artifact_id,
            revision,
            expected_kind="configuration",
            label="execution authorization",
        )
        scope = body.get("scope")
        target_snapshot = body.get("execution_target")
        expires_at = _aware_time(reference.get("expires_at"), "authorization expiry")
        if (
            body.get("authorization_id") != artifact_id
            or body.get("authorization_kind") != _PREPARED_AUTHORIZATION_KIND
            or reference.get("authorization_kind") != _PREPARED_AUTHORIZATION_KIND
            or body.get("status") != "active"
            or body.get("task_id") != task["id"]
            or body.get("expires_at") != reference.get("expires_at")
            or not isinstance(scope, Mapping)
            or not isinstance(target_snapshot, Mapping)
        ):
            raise GovernedPreparationError(
                "execution authorization body conflicts with PROJECT_STATE"
            )
        if calibration is not None and scope.get("parallel_efficiency_calibration") != task.get(
            "parallel_efficiency_calibration"
        ):
            raise GovernedPreparationError(
                "execution authorization does not bind the parallel-efficiency calibration"
            )
        required_until = now + timedelta(
            seconds=budget["max_wall_seconds"] + _LAUNCH_MARGIN_SECONDS
        )
        if expires_at <= required_until:
            raise GovernedPreparationError(
                "execution authorization cannot cover the prepared session wall time"
            )
        if reference_target not in targets:
            raise GovernedPreparationError("authorization target is not executable")
        target = targets[reference_target]
        if (
            scope.get("target_id") != reference_target
            or target_snapshot.get("target_id") != reference_target
            or target_snapshot.get("status") != target.get("status")
            or target_snapshot.get("formal_execution") is not True
            or target_snapshot.get("workspace_root") != target.get("workspace_root")
            or not isinstance(target_snapshot.get("allowed_operations"), list)
            or "simulation" not in target_snapshot["allowed_operations"]
            or scope.get("execution_targets_revision") != targets_revision
        ):
            raise GovernedPreparationError(
                "execution authorization target snapshot is inconsistent"
            )
        target_options = [
            option for option in options if option["target_id"] == reference_target
        ]
        max_timeout = scope.get("max_timeout_seconds")
        max_attempts = scope.get("max_attempts_per_candidate")
        allowed_processors = scope.get("allowed_processors")
        max_memory = scope.get("max_memory_bytes")
        if (
            isinstance(max_timeout, bool)
            or not isinstance(max_timeout, int)
            or budget["command_timeout_seconds"] > max_timeout
            or isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or budget["max_solver_runs"] > max_attempts
            or not isinstance(allowed_processors, list)
            or not allowed_processors
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 1
                for value in allowed_processors
            )
            or len(allowed_processors) != len(set(allowed_processors))
            or isinstance(max_memory, bool)
            or not isinstance(max_memory, int)
            or max_memory < 1
            or any(
                option["processors"] not in allowed_processors
                or option["memory_bytes"] > max_memory
                for option in target_options
            )
        ):
            raise GovernedPreparationError(
                "execution preparation exceeds its authorization budget"
            )
        records.append({
            "reference": reference,
            "body": body,
            "scope": scope,
            "target_id": reference_target,
        })

    if option_targets != seen_targets:
        raise GovernedPreparationError(
            "execution option targets exceed the active authorization"
            if len(seen_targets) == 1
            else "execution option targets must exactly match active authorizations"
        )
    return records


def _formal_targets(
    project_root: Path, target_catalog: TargetCatalog | None = None
) -> dict[str, Mapping[str, Any]]:
    catalog = target_catalog or ProjectFileTargetCatalog()
    try:
        values = catalog.read_targets(project_root)
    except (OSError, ValueError) as exc:
        raise GovernedPreparationError("cannot read execution targets") from exc
    if not isinstance(values, Sequence):
        raise GovernedPreparationError("execution targets must be an array")
    targets: dict[str, Mapping[str, Any]] = {}
    for item in values:
        if not isinstance(item, Mapping):
            raise GovernedPreparationError("execution target is invalid")
        target_id = str(item.get("target_id", ""))
        if target_id in targets:
            raise GovernedPreparationError("execution target IDs must be unique")
        allowed_operations = item.get("allowed_operations")
        if (
            item.get("status") == "active"
            and item.get("formal_execution") is True
            and isinstance(allowed_operations, list)
            and "simulation" in allowed_operations
        ):
            targets[target_id] = item
    if not targets:
        raise GovernedPreparationError(
            "project has no active formal simulation target"
        )
    return targets


def _validate_resource_neutral_package(
    package_root: Path, manifest: Mapping[str, Any]
) -> None:
    execution = manifest.get("execution")
    if (
        execution is not None and not isinstance(execution, Mapping)
    ) or (
        isinstance(execution, Mapping)
        and any(
            field in execution for field in ("processors", "atlas_processors")
        )
    ):
        raise GovernedPreparationError(
            "project-scheduled package must not freeze a CPU shape"
        )
    # Adapter-specific deck semantics are validated by SimulationAdapter.
    return


def _authorization_reference_for_target(
    authorization: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    target_id: str,
) -> Mapping[str, Any] | None:
    if isinstance(authorization, Mapping):
        return authorization
    for record in authorization:
        if not isinstance(record, Mapping):
            continue
        if record.get("target_id") == target_id:
            reference = record.get("reference")
            return reference if isinstance(reference, Mapping) else record
    return None


def _validate_options(
    project_root: Path,
    task: Mapping[str, Any],
    preparation: Mapping[str, Any],
    authorization: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    require_resource_neutral_package: bool = False,
) -> None:
    approvals = task.get("approved_packages")
    if not require_resource_neutral_package and not isinstance(approvals, list):
        raise GovernedPreparationError(
            "prepared execution task lacks package approvals"
        )

    definitions: set[tuple[str, str]] = set()
    checked_packages: set[tuple[str, str]] = set()
    options = preparation["execution_option_set"]["options"]
    package_processors: dict[tuple[str, str], set[int]] = {}
    for option in options:
        package = option["runnable_package"]
        package_key = (package["artifact_id"], package["revision"])
        package_processors.setdefault(package_key, set()).add(
            option["processors"]
        )
    if (
        not require_resource_neutral_package
        and any(len(values) != 1 for values in package_processors.values())
    ):
        raise GovernedPreparationError(
            "one immutable runnable package cannot advertise multiple CPU shapes"
        )

    for option in options:
        definition = option["simulation_definition"]
        definitions.add((definition["artifact_id"], definition["revision"]))
        package = option["runnable_package"]
        package_key = (package["artifact_id"], package["revision"])
        if package_key in checked_packages:
            continue
        checked_packages.add(package_key)
        if not require_resource_neutral_package:
            matches = [
                item
                for item in approvals
                if isinstance(item, Mapping)
                and item.get("artifact_id") == package["artifact_id"]
                and str(item.get("revision", "")).lower() == package["revision"]
            ]
            if len(matches) != 1:
                raise GovernedPreparationError(
                    "execution option package is not exactly approved for its task"
                )
            approval = matches[0]
            parent = approval.get("parent_authorization")
            option_authorization = _authorization_reference_for_target(
                authorization, option["target_id"]
            )
            if (
                approval.get("evaluation_id") != preparation["evaluation_id"]
                or approval.get("physical_candidate_id") != preparation["candidate_id"]
                or not isinstance(parent, Mapping)
                or not isinstance(option_authorization, Mapping)
                or parent.get("artifact_id") != option_authorization.get("artifact_id")
                or str(parent.get("revision", "")).lower()
                != str(option_authorization.get("revision", "")).lower()
            ):
                raise GovernedPreparationError(
                    "execution option package approval is not bound to this evaluation"
                )
        try:
            resolved = resolve_workspace_artifact(
                project_root,
                package["artifact_id"],
                revision=package["revision"],
                expected_kind="input-package",
            )
        except WorkspaceArtifactError as exc:
            raise GovernedPreparationError(
                "execution option package is not an exact active artifact"
            ) from exc
        if resolved.hash_scope != "package-manifest":
            raise GovernedPreparationError(
                "execution option package must use package-manifest hash scope"
            )
        manifest = _read_json(resolved.path / "manifest.json", "package manifest")
        design = manifest.get("design")
        execution = manifest.get("execution")
        if (
            manifest.get("artifact_id") != package["artifact_id"]
            or not isinstance(design, Mapping)
            or design.get("candidate_id") != preparation["candidate_id"]
        ):
            raise GovernedPreparationError(
                "execution option package conflicts with Candidate"
            )
        if require_resource_neutral_package:
            _validate_resource_neutral_package(resolved.path, manifest)
        else:
            expected_processors = next(iter(package_processors[package_key]))
            if (
                not isinstance(execution, Mapping)
                or execution.get("processors") != expected_processors
            ):
                raise GovernedPreparationError(
                    "execution option package conflicts with CPU shape"
                )

    if len(definitions) != 1:
        raise GovernedPreparationError(
            "execution options do not share one SimulationDefinition"
        )
    definition_id, definition_revision = definitions.pop()
    try:
        definition = resolve_workspace_artifact(
            project_root,
            definition_id,
            revision=definition_revision,
            expected_kind="configuration",
        )
    except WorkspaceArtifactError as exc:
        raise GovernedPreparationError(
            "SimulationDefinition is not an exact active configuration"
        ) from exc
    if definition.hash_scope != "file":
        raise GovernedPreparationError(
            "SimulationDefinition must use file hash scope"
        )

    evidence_documents: dict[tuple[str, str], Mapping[str, Any]] = {}
    for profile in preparation["performance_profile_snapshot"]["profiles"]:
        evidence = profile["evidence"]
        evidence_key = (evidence["artifact_id"], evidence["revision"])
        document = evidence_documents.get(evidence_key)
        if document is None:
            try:
                resolved = resolve_workspace_artifact(
                    project_root,
                    evidence["artifact_id"],
                    revision=evidence["revision"],
                    expected_kind="evidence",
                )
            except WorkspaceArtifactError as exc:
                raise GovernedPreparationError(
                    "performance profile evidence is not an exact active artifact"
                ) from exc
            if resolved.hash_scope != "file":
                raise GovernedPreparationError(
                    "performance profile evidence must use file hash scope"
                )
            document = _read_json(
                resolved.path, "execution performance evidence"
            )
            if (
                document.get("schema_version") != 1
                or document.get("evidence_kind")
                != "execution-performance-evidence"
                or not isinstance(document.get("profiles"), list)
            ):
                raise GovernedPreparationError(
                    "execution performance evidence contract is invalid"
                )
            evidence_documents[evidence_key] = document
        expected = {
            "performance_class_id": profile["performance_class_id"],
            "sample_count": profile["sample_count"],
            "duration_p50_seconds": profile["duration_p50_seconds"],
            "duration_p90_seconds": profile["duration_p90_seconds"],
            "peak_rss_p90_bytes": profile["peak_rss_p90_bytes"],
            "success_rate_ppm": profile["success_rate_ppm"],
        }
        matches = [
            row
            for row in document["profiles"]
            if isinstance(row, Mapping)
            and all(row.get(key) == value for key, value in expected.items())
        ]
        if len(matches) != 1:
            raise GovernedPreparationError(
                "performance profile does not match its exact evidence artifact"
            )


def _policy_task(
    state: Mapping[str, Any], task_id: str
) -> Mapping[str, Any]:
    if state.get("schema_version") != 2 or state.get("status") != "active":
        raise GovernedPreparationError(
            "PROJECT_STATE must be active schema version 2"
        )
    tasks = state.get("active_tasks")
    if not isinstance(tasks, list):
        raise GovernedPreparationError(
            "PROJECT_STATE active_tasks must be an array"
        )
    matches = [
        task
        for task in tasks
        if isinstance(task, Mapping) and task.get("id") == task_id
    ]
    if len(matches) != 1:
        raise GovernedPreparationError(
            "policy-derived Preparation task is not uniquely active"
        )
    task = matches[0]
    if (
        task.get("kind") != "simulation"
        or task.get("status") != _EXECUTABLE_TASK_STATUS
    ):
        raise GovernedPreparationError(
            "policy-derived Preparation task is not approved for execution"
        )
    return task


def _parallel_efficiency_calibration(
    task: Mapping[str, Any], preparation: Mapping[str, Any]
) -> dict[str, Any] | None:
    calibration = preparation.get("calibration")
    if calibration is None:
        return None
    try:
        configuration = validate_parallel_efficiency_calibration_configuration(
            task.get("parallel_efficiency_calibration"),
            candidate_id=preparation.get("candidate_id"),
        )
        normalized = validate_parallel_efficiency_calibration(calibration)
        ordinal = normalized["replicate_ordinal"]
        expected = make_parallel_efficiency_calibration(
            replicate_ordinal=ordinal,
            selected_processors=configuration["processor_sequence"][ordinal - 1],
            unmeasured_processors=configuration["unmeasured_processors"],
            target_isolation=configuration["target_isolation"],
        )
    except (
        ExecutionOptionError,
        IndexError,
        ParallelEfficiencyCalibrationError,
    ) as exc:
        raise GovernedPreparationError(
            "parallel-efficiency calibration is invalid"
        ) from exc
    if normalized != expected:
        raise GovernedPreparationError(
            "parallel-efficiency calibration differs from its authorized sequence"
        )
    return normalized

def _validate_policy_derived_preparation(
    project_root: Path,
    preparation: Mapping[str, Any],
    policy: GovernedSchedulingPolicy,
    *,
    now: datetime | None,
    control_store: ControlStore | None = None,
    target_catalog: TargetCatalog | None = None,
) -> tuple[dict[str, Any], str]:
    if not isinstance(policy, GovernedSchedulingPolicy) or not policy.is_attested_for(
        project_root
    ):
        raise GovernedPreparationError(
            "SchedulingPolicy must come from the project resolver"
        )
    try:
        current_policy = resolve_governed_scheduling_policy(
            project_root, control_store=control_store
        )
    except SchedulingPolicyError as exc:
        raise GovernedPreparationError(
            "project SchedulingPolicy is unavailable"
        ) from exc
    if current_policy.provenance() != policy.provenance():
        raise GovernedPreparationError(
            "SchedulingPolicy changed during Preparation materialization"
        )
    try:
        normalized = validate_execution_preparation(preparation)
    except ExecutionOptionError as exc:
        raise GovernedPreparationError(
            "policy-derived execution preparation is invalid"
        ) from exc

    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        raise GovernedPreparationError("governance time must include a timezone")
    targets = _formal_targets(project_root, target_catalog)
    target_ids = {str(item["target_id"]) for item in targets}
    options = normalized["execution_option_set"]["options"]
    if any(str(option["target_id"]) not in target_ids for option in options):
        raise GovernedPreparationError("execution option target is not active in catalog")
    capacity = policy.as_mapping()["capacity_envelope"]
    if any(
        option["processors"] > capacity["processors"]
        or option["memory_bytes"] > capacity["memory_bytes"]
        for option in options
    ):
        raise GovernedPreparationError(
            "execution option exceeds the SchedulingPolicy capacity envelope"
        )
    return normalized, ""
def attest_policy_derived_execution_preparation(
    project_root: Path | str,
    evaluation_input: Mapping[str, Any],
    preparation: Mapping[str, Any],
    policy: GovernedSchedulingPolicy,
    *,
    now: datetime | None = None,
    control_store: ControlStore | None = None,
    target_catalog: TargetCatalog | None = None,
) -> GovernedExecutionPreparation:
    """Seal one built-in materializer result under the exact project policy."""

    root = Path(project_root).resolve()
    normalized, state_revision = _validate_policy_derived_preparation(
        root, preparation, policy, now=now,
        control_store=control_store, target_catalog=target_catalog,
    )
    if not isinstance(evaluation_input, Mapping):
        raise GovernedPreparationError("evaluation input must be an object")
    candidate = evaluation_input.get("candidate")
    evaluation = evaluation_input.get("evaluation")
    if (
        not isinstance(candidate, Mapping)
        or not isinstance(evaluation, Mapping)
        or evaluation.get("status") != "queued"
        or evaluation.get("evaluation_id") != normalized["evaluation_id"]
        or evaluation.get("candidate_id") != normalized["candidate_id"]
        or candidate.get("candidate_id") != normalized["candidate_id"]
    ):
        raise GovernedPreparationError(
            "policy-derived Preparation conflicts with its Evaluation input"
        )
    provenance = policy.provenance()
    return GovernedExecutionPreparation(
        normalized,
        project_root=root,
        artifact_id=provenance["artifact_id"],
        artifact_revision=provenance["revision"],
        project_state_revision=state_revision,
        _seal=_GOVERNANCE_SEAL,
    )


def validate_policy_derived_execution_preparation(
    project_root: Path | str,
    preparation: Mapping[str, Any],
    *,
    now: datetime | None = None,
    control_store: ControlStore | None = None,
    target_catalog: TargetCatalog | None = None,
) -> dict[str, Any]:
    """Revalidate a queued policy-derived Preparation before allocation."""

    root = Path(project_root).resolve()
    try:
        policy = resolve_governed_scheduling_policy(
            root, control_store=control_store
        )
    except SchedulingPolicyError as exc:
        raise GovernedPreparationError(
            "project SchedulingPolicy is unavailable"
        ) from exc
    normalized, _ = _validate_policy_derived_preparation(
        root, preparation, policy, now=now,
        control_store=control_store, target_catalog=target_catalog,
    )
    return normalized
