#!/usr/bin/env python3
"""Contract checks for immutable execution choices."""

from __future__ import annotations

import unittest

from control_plane.evaluation.execution_options import (
    ExecutionOptionError,
    make_execution_preparation,
    make_execution_option,
    make_execution_option_set,
    make_parallel_efficiency_calibration,
    make_performance_profile,
    make_performance_profile_snapshot,
    validate_execution_preparation,
    validate_execution_option,
    validate_execution_option_set,
)


REVISION = "sha256:" + "a" * 64
PERFORMANCE_CLASS = "performance-class:sha256:" + "b" * 64
OTHER_PERFORMANCE_CLASS = "performance-class:sha256:" + "c" * 64


def option(
    name: str,
    processors: int,
    *,
    performance_class_id: str = PERFORMANCE_CLASS,
) -> dict:
    return make_execution_option(
        simulation_definition_artifact_id="simulation-definition.fixture",
        simulation_definition_revision=REVISION,
        runnable_package_artifact_id=f"package.{name}",
        runnable_package_revision=REVISION,
        target_id="simulation.remote-primary",
        processors=processors,
        memory_bytes=4 * 1024**3,
        performance_class_id=performance_class_id,
    )


def profile(value: dict, duration: int) -> dict:
    return make_performance_profile(
        execution_option_id=value["option_id"],
        evidence_artifact_id="evidence.performance.fixture",
        evidence_revision=REVISION,
        sample_count=3,
        duration_p50_seconds=duration,
        duration_p90_seconds=duration + 10,
        peak_rss_p90_bytes=2 * 1024**3,
        performance_class_id=value["performance_class_id"],
    )


class ExecutionOptionTests(unittest.TestCase):
    def test_option_and_set_are_content_addressed(self) -> None:
        p1 = option("p1", 1)
        p2 = option("p2", 2)

        self.assertEqual(validate_execution_option(p1), p1)
        self.assertEqual(
            make_execution_option_set([p1, p2]),
            make_execution_option_set([p2, p1]),
        )
        self.assertEqual(
            validate_execution_option_set(make_execution_option_set([p1, p2])),
            make_execution_option_set([p1, p2]),
        )

    def test_mutated_option_and_duplicate_set_are_rejected(self) -> None:
        p1 = option("p1", 1)
        mutated = {**p1, "processors": 2}

        with self.assertRaises(ExecutionOptionError):
            validate_execution_option(mutated)
        with self.assertRaises(ExecutionOptionError):
            make_execution_option_set([p1, p1])

    def test_option_set_separates_science_from_runnable_variants(self) -> None:
        p1 = option("p1", 1)
        p4 = option("p4", 4)
        options = make_execution_option_set([p1, p4])

        self.assertEqual(
            p1["simulation_definition"], p4["simulation_definition"]
        )
        self.assertNotEqual(p1["runnable_package"], p4["runnable_package"])
        self.assertEqual(len(options["options"]), 2)

        other_science = make_execution_option(
            simulation_definition_artifact_id="simulation-definition.other",
            simulation_definition_revision=REVISION,
            runnable_package_artifact_id="package.other",
            runnable_package_revision=REVISION,
            target_id="simulation.remote-primary",
            processors=1,
            memory_bytes=4 * 1024**3,
            performance_class_id=PERFORMANCE_CLASS,
        )
        with self.assertRaisesRegex(
            ExecutionOptionError, "share one simulation definition"
        ):
            make_execution_option_set([p1, other_science])

    def test_preparation_keeps_profiles_out_of_option_identity(self) -> None:
        p1 = option("p1", 1)
        p2 = option("p2", 2)
        options = make_execution_option_set([p1, p2])
        profiles = make_performance_profile_snapshot(
            policy_revision=REVISION,
            profiles=[profile(p1, 100), profile(p2, 60)],
        )
        preparation = make_execution_preparation(
            evaluation_id="evaluation:fixture",
            candidate_id="candidate:sha256:" + "1" * 64,
            simulation_proxy="simulation-session-v1",
            numerical_profile="proxy-managed-v1",
            recovery_profile_revision=REVISION,
            command_timeout_seconds=600,
            max_solver_runs=1,
            max_wall_seconds=900,
            execution_option_set=options,
            performance_profile_snapshot=profiles,
        )

        self.assertEqual(validate_execution_preparation(preparation), preparation)
        updated_profile = profile(p1, 90)
        self.assertEqual(updated_profile["execution_option_id"], p1["option_id"])
        self.assertNotEqual(updated_profile["profile_id"], profile(p1, 100)["profile_id"])

    def _preparation_kwargs(self) -> dict:
        p1 = option("p1", 1)
        return {
            "evaluation_id": "evaluation:fixture",
            "candidate_id": "candidate:sha256:" + "1" * 64,
            "simulation_proxy": "simulation-session-v1",
            "numerical_profile": "proxy-managed-v1",
            "recovery_profile_revision": REVISION,
            "command_timeout_seconds": 600,
            "max_solver_runs": 1,
            "max_wall_seconds": 900,
            "execution_option_set": make_execution_option_set([p1]),
            "performance_profile_snapshot": make_performance_profile_snapshot(
                policy_revision=REVISION, profiles=[profile(p1, 100)]
            ),
        }

    def test_preparation_round_trips_without_authorization_lineage(self) -> None:
        preparation = make_execution_preparation(**self._preparation_kwargs())
        self.assertEqual(validate_execution_preparation(preparation), preparation)
        for removed in ("authorizations", "authorization", "task_id",
                        "authorization_id", "authorization_revision"):
            self.assertNotIn(removed, preparation)

    def test_preparation_rejects_legacy_authorization_and_task_fields(self) -> None:
        cases = (
            ("authorizations", []),
            ("authorization", {"artifact_id": "authorization.fixture", "revision": REVISION}),
            ("task_id", "fixture-task"),
            ("authorization_id", "authorization.fixture"),
            ("authorization_revision", REVISION),
        )
        for field, value in cases:
            with self.subTest(field=field):
                with self.assertRaises(ExecutionOptionError):
                    validate_execution_preparation(
                        {**make_execution_preparation(**self._preparation_kwargs()),
                         field: value}
                    )

    def test_single_authorization_body_has_legacy_shape(self) -> None:
        preparation = make_execution_preparation(**self._preparation_kwargs())
        self.assertNotIn("authorizations", preparation)
        self.assertNotIn("authorization", preparation)
        again = make_execution_preparation(**self._preparation_kwargs())
        self.assertEqual(
            __import__("json").dumps(preparation, sort_keys=True, separators=(",", ":")),
            __import__("json").dumps(again, sort_keys=True, separators=(",", ":")),
        )

    def test_preparation_rejects_missing_option_profile(self) -> None:
        p1 = option("p1", 1)
        p2 = option("p2", 2)
        with self.assertRaises(ExecutionOptionError):
            make_execution_preparation(
                evaluation_id="evaluation:fixture",
                candidate_id="candidate:sha256:" + "1" * 64,
                simulation_proxy="simulation-session-v1",
                numerical_profile="proxy-managed-v1",
                recovery_profile_revision=REVISION,
                command_timeout_seconds=600,
                max_solver_runs=1,
                max_wall_seconds=900,
                execution_option_set=make_execution_option_set([p1, p2]),
                performance_profile_snapshot=make_performance_profile_snapshot(
                    policy_revision=REVISION,
                    profiles=[profile(p1, 100)],
                ),
            )

    def test_performance_class_is_stable_and_part_of_both_contracts(self) -> None:
        p1 = option("p1", 1)
        measured = profile(p1, 100)

        self.assertEqual(p1["performance_class_id"], PERFORMANCE_CLASS)
        self.assertEqual(measured["performance_class_id"], PERFORMANCE_CLASS)
        self.assertNotEqual(
            p1["option_id"],
            option(
                "p1",
                1,
                performance_class_id=OTHER_PERFORMANCE_CLASS,
            )["option_id"],
        )
        with self.assertRaisesRegex(
            ExecutionOptionError, "performance-class SHA-256 identity"
        ):
            option("invalid", 1, performance_class_id="performance-class:invalid")

    def test_preparation_rejects_option_profile_class_mismatch(self) -> None:
        p1 = option("p1", 1)
        mismatched = make_performance_profile(
            execution_option_id=p1["option_id"],
            evidence_artifact_id="evidence.performance.fixture",
            evidence_revision=REVISION,
            sample_count=3,
            duration_p50_seconds=100,
            duration_p90_seconds=110,
            peak_rss_p90_bytes=2 * 1024**3,
            performance_class_id=OTHER_PERFORMANCE_CLASS,
        )

        with self.assertRaisesRegex(ExecutionOptionError, "classes must match"):
            make_execution_preparation(
                evaluation_id="evaluation:fixture",
                candidate_id="candidate:sha256:" + "1" * 64,
                simulation_proxy="simulation-session-v1",
                numerical_profile="proxy-managed-v1",
                recovery_profile_revision=REVISION,
                command_timeout_seconds=600,
                max_solver_runs=1,
                max_wall_seconds=900,
                execution_option_set=make_execution_option_set([p1]),
                performance_profile_snapshot=make_performance_profile_snapshot(
                    policy_revision=REVISION,
                    profiles=[mismatched],
                ),
            )

    def test_multi_processor_option_requires_measured_profile(self) -> None:
        p2 = option("p2", 2)
        unmeasured = make_performance_profile(
            execution_option_id=p2["option_id"],
            evidence_artifact_id="evidence.performance.fixture",
            evidence_revision=REVISION,
            sample_count=0,
            duration_p50_seconds=60,
            duration_p90_seconds=70,
            peak_rss_p90_bytes=2 * 1024**3,
            performance_class_id=p2["performance_class_id"],
        )

        with self.assertRaisesRegex(
            ExecutionOptionError, "require measured performance evidence"
        ):
            make_execution_preparation(
                evaluation_id="evaluation:fixture",
                candidate_id="candidate:sha256:" + "1" * 64,
                simulation_proxy="simulation-session-v1",
                numerical_profile="proxy-managed-v1",
                recovery_profile_revision=REVISION,
                command_timeout_seconds=600,
                max_solver_runs=1,
                max_wall_seconds=900,
                execution_option_set=make_execution_option_set([p2]),
                performance_profile_snapshot=make_performance_profile_snapshot(
                    policy_revision=REVISION,
                    profiles=[unmeasured],
                ),
            )

    def test_calibration_can_attest_exact_unmeasured_parallel_shapes(self) -> None:
        p1 = option("p1", 1)
        p2 = option("p2", 2)
        p4 = option("p4", 4)

        def unmeasured(value: dict, duration: int) -> dict:
            return make_performance_profile(
                execution_option_id=value["option_id"],
                evidence_artifact_id="evidence.performance.fixture",
                evidence_revision=REVISION,
                sample_count=0,
                duration_p50_seconds=duration,
                duration_p90_seconds=duration + 10,
                peak_rss_p90_bytes=2 * 1024**3,
                performance_class_id=value["performance_class_id"],
            )

        calibration = make_parallel_efficiency_calibration(
            replicate_ordinal=2,
            selected_processors=2,
            unmeasured_processors=[2, 4],
        )
        preparation = make_execution_preparation(
            evaluation_id="evaluation:fixture",
            candidate_id="candidate:sha256:" + "1" * 64,
            simulation_proxy="simulation-session-v1",
            numerical_profile="proxy-managed-v1",
            recovery_profile_revision=REVISION,
            command_timeout_seconds=600,
            max_solver_runs=1,
            max_wall_seconds=900,
            execution_option_set=make_execution_option_set([p1, p2, p4]),
            performance_profile_snapshot=make_performance_profile_snapshot(
                policy_revision=REVISION,
                profiles=[profile(p1, 100), unmeasured(p2, 70), unmeasured(p4, 60)],
            ),
            calibration=calibration,
        )

        self.assertEqual(preparation["calibration"], calibration)
        self.assertEqual(validate_execution_preparation(preparation), preparation)
        with self.assertRaisesRegex(
            ExecutionOptionError, "requires exactly one solver run"
        ):
            validate_execution_preparation(
                {
                    **preparation,
                    "budget": {**preparation["budget"], "max_solver_runs": 2},
                }
            )


if __name__ == "__main__":
    unittest.main()
