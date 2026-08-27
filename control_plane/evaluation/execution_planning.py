"""Materialize a SessionPlan only after the Scheduler selects an option."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from control_plane.evaluation.execution_options import (
    ExecutionOptionError,
    validate_execution_option,
    validate_execution_preparation,
)
from control_plane.simulation.session_contracts import make_simulation_session_plan


def materialize_session_plan(
    *,
    attempt_id: str,
    preparation: Mapping[str, Any],
    selected_option: Mapping[str, Any],
) -> dict[str, Any]:
    """Create the exact immutable plan selected by one scheduling decision."""

    normalized_preparation = validate_execution_preparation(preparation)
    normalized_option = validate_execution_option(selected_option)
    offered = {
        item["option_id"]: item
        for item in normalized_preparation["execution_option_set"]["options"]
    }
    if offered.get(normalized_option["option_id"]) != normalized_option:
        raise ExecutionOptionError(
            "selected execution option is not part of the preparation"
        )
    package = normalized_option["runnable_package"]
    authorization = normalized_preparation["authorization"]
    authorizations = normalized_preparation.get("authorizations")
    if authorizations is not None:
        if not isinstance(authorizations, list):
            raise ExecutionOptionError(
                "execution preparation authorization lineage is invalid"
            )
        matches = [
            item
            for item in authorizations
            if isinstance(item, Mapping)
            and item.get("target_id") == normalized_option["target_id"]
        ]
        if len(matches) != 1:
            raise ExecutionOptionError(
                "selected execution option has no unique authorization"
            )
        authorization = matches[0]
    budget = normalized_preparation["budget"]
    return make_simulation_session_plan(
        attempt_id=attempt_id,
        evaluation_id=normalized_preparation["evaluation_id"],
        candidate_id=normalized_preparation["candidate_id"],
        simulation_proxy=normalized_preparation["simulation_proxy"],
        recovery_profile_revision=normalized_preparation[
            "recovery_profile_revision"
        ],
        base_package_artifact_id=package["artifact_id"],
        base_package_revision=package["revision"],
        task_id=normalized_preparation["task_id"],
        target_id=normalized_option["target_id"],
        authorization_id=authorization["artifact_id"],
        authorization_revision=authorization["revision"],
        requested_processors=normalized_option["processors"],
        command_timeout_seconds=budget["command_timeout_seconds"],
        max_solver_runs=budget["max_solver_runs"],
        max_wall_seconds=budget["max_wall_seconds"],
    )
