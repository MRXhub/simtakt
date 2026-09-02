#!/usr/bin/env python3
"""Checks for target-specific SessionPlan materialization."""

from __future__ import annotations

import unittest

from control_plane.evaluation.execution_options import (
    ExecutionOptionError,
    make_execution_option,
    make_execution_option_set,
    make_execution_preparation,
    make_performance_profile,
    make_performance_profile_snapshot,
)
from control_plane.evaluation.execution_planning import materialize_session_plan


REVISION = "sha256:" + "a" * 64
PERFORMANCE_CLASS = "performance-class:sha256:" + "b" * 64
CANDIDATE_ID = "candidate:sha256:" + "1" * 64


class ExecutionPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        options = []
        profiles = []
        for target, package, processors in (
            ("target-a", "package.a", 1),
            ("target-b", "package.b", 2),
        ):
            item = make_execution_option(
                simulation_definition_artifact_id="simulation-definition.fixture",
                simulation_definition_revision=REVISION,
                runnable_package_artifact_id=package,
                runnable_package_revision=REVISION,
                target_id=target,
                processors=processors,
                memory_bytes=4 * 1024**3,
                performance_class_id=PERFORMANCE_CLASS,
            )
            options.append(item)
            profiles.append(
                make_performance_profile(
                    execution_option_id=item["option_id"],
                    evidence_artifact_id="evidence.fixture",
                    evidence_revision=REVISION,
                    sample_count=2,
                    duration_p50_seconds=100,
                    duration_p90_seconds=120,
                    peak_rss_p90_bytes=2 * 1024**3,
                    performance_class_id=PERFORMANCE_CLASS,
                )
            )
        self.options = options
        self.preparation = make_execution_preparation(
            evaluation_id="evaluation:22222222-2222-4222-8222-222222222222",
            candidate_id=CANDIDATE_ID,
            simulation_proxy="simulation-session-v1",
            numerical_profile="proxy-managed-v1",
            recovery_profile_revision=REVISION,
            command_timeout_seconds=600,
            max_solver_runs=1,
            max_wall_seconds=900,
            execution_option_set=make_execution_option_set(options),
            performance_profile_snapshot=make_performance_profile_snapshot(
                policy_revision=REVISION, profiles=profiles
            ),
            authorizations=[
                {"artifact_id": "authorization.a", "revision": REVISION, "target_id": "target-a"},
                {"artifact_id": "authorization.b", "revision": REVISION, "target_id": "target-b"},
            ],
        )

    def test_selected_target_uses_its_authorization(self) -> None:
        plan = materialize_session_plan(
            attempt_id="attempt:11111111-1111-4111-8111-111111111111",
            preparation=self.preparation,
            selected_option=self.options[1],
        )
        # The SessionPlan contract intentionally stores an artifact reference,
        # while the Preparation lineage carries the routing target_id.
        self.assertEqual(
            plan["authorization"], {"artifact_id": "authorization.b", "revision": REVISION}
        )
        self.assertEqual(plan["target_id"], "target-b")

    def test_selected_target_requires_one_authorization(self) -> None:
        unmatched = make_execution_option(
            simulation_definition_artifact_id="simulation-definition.fixture",
            simulation_definition_revision=REVISION,
            runnable_package_artifact_id="package.unmatched",
            runnable_package_revision=REVISION,
            target_id="target-c",
            processors=1,
            memory_bytes=4 * 1024**3,
            performance_class_id=PERFORMANCE_CLASS,
        )
        no_match = make_execution_preparation(
            evaluation_id="evaluation:22222222-2222-4222-8222-222222222222",
            candidate_id=CANDIDATE_ID,
            simulation_proxy="simulation-session-v1",
            numerical_profile="proxy-managed-v1",
            recovery_profile_revision=REVISION,
            command_timeout_seconds=600,
            max_solver_runs=1,
            max_wall_seconds=900,
            execution_option_set=self.preparation["execution_option_set"],
            performance_profile_snapshot=self.preparation["performance_profile_snapshot"],
            authorizations=[
                {"artifact_id": "authorization.a", "revision": REVISION, "target_id": "target-a"},
                {"artifact_id": "authorization.c", "revision": REVISION, "target_id": "target-c"},
            ],
        )
        with self.assertRaisesRegex(ExecutionOptionError, "no unique authorization"):
            materialize_session_plan(
                attempt_id="attempt:11111111-1111-4111-8111-111111111111",
                preparation=no_match,
                selected_option=self.options[1],
            )

        duplicate = make_execution_preparation(
            evaluation_id="evaluation:22222222-2222-4222-8222-222222222222",
            candidate_id=CANDIDATE_ID,
            simulation_proxy="simulation-session-v1",
            numerical_profile="proxy-managed-v1",
            recovery_profile_revision=REVISION,
            command_timeout_seconds=600,
            max_solver_runs=1,
            max_wall_seconds=900,
            execution_option_set=self.preparation["execution_option_set"],
            performance_profile_snapshot=self.preparation["performance_profile_snapshot"],
            authorizations=[
                {"artifact_id": "authorization.a", "revision": REVISION, "target_id": "target-a"},
                {"artifact_id": "authorization.b", "revision": REVISION, "target_id": "target-b"},
                {"artifact_id": "authorization.c", "revision": REVISION, "target_id": "target-b"},
            ],
        )
        with self.assertRaisesRegex(ExecutionOptionError, "no unique authorization"):
            materialize_session_plan(
                attempt_id="attempt:11111111-1111-4111-8111-111111111111",
                preparation=duplicate,
                selected_option=self.options[1],
            )


if __name__ == "__main__":
    unittest.main()
