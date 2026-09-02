#!/usr/bin/env python3
"""Repository checks for the finite rolling Preparation window."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from control_plane.core.evaluation_contracts import (
    ACTIVE_ATTEMPT_STATES,
    ATTEMPT_STATES,
    ATTEMPT_TERMINATION_STATES,
    CAPACITY_HOLDING_ATTEMPT_STATES,
    HEARTBEATABLE_ATTEMPT_STATES,
    TERMINATION_REQUEST_SOURCE_STATES,
    _ATTEMPT_STATE_SQL_ORDER,
    attempt_states_sql,
    attempt_termination_states_sql,
    make_candidate,
    make_evaluation_request,
    make_problem_definition,
)
from control_plane.data.sqlite_evaluation_repository import (
    SCHEMA_VERSION,
    RepositoryError,
    SQLiteEvaluationRepository,
)
from control_plane.evaluation.compute_profile import MIN_TERMINAL_COMPLETION_SAMPLES
from control_plane.evaluation.automation_policy import DEFAULT_AUTOMATION_POLICY
from control_plane.evaluation.execution_options import (
    make_execution_option,
    make_execution_option_set,
    make_execution_preparation,
    make_performance_profile,
    make_performance_profile_snapshot,
)
from control_plane.evaluation.execution_planning import materialize_session_plan
from control_plane.evaluation.dispatcher import SessionLifecycleDispatcher
from control_plane.evaluation.service import EvaluationMiddleware
from control_plane.evaluation.scheduling import make_resource_allocation, schedule


REVISION = "sha256:" + "1" * 64
PERFORMANCE_CLASS_ID = "performance-class:sha256:" + "2" * 64
TARGET = "simulation.remote-primary"
OTHER_TARGET = "simulation.remote-secondary"
CONTROLLER = "scheduling-controller:fixture"
WORKER = "worker:fixture"
BASE_TIME = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)


class RollingWindowRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "control.sqlite3"
        self.repository = SQLiteEvaluationRepository(self.database)
        from tests.shared_fixtures import register_fixture_schema
        schema_revision = register_fixture_schema(self.repository, problem_hint="rolling-window")
        self.problem = make_problem_definition(
            problem_id="rolling-window-fixture",
            parameter_schema_revision=schema_revision,
            constraint_revision=REVISION,
            simulation_capabilities=["full-tcad"],
            metric_schema_revision=REVISION,
        )
        self.repository.register_problem(self.problem)
        self.submission_count = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def submit(self, *, priority: str = "normal") -> dict:
        self.submission_count += 1
        candidate = make_candidate(
            problem_id=self.problem["problem_id"],
            problem_revision=self.problem["revision"],
            parameters={"x": float(self.submission_count)},
        )
        request = make_evaluation_request(
            candidate_id=candidate["candidate_id"],
            fidelity="full-tcad",
            requested_outputs=["Eff"],
            evidence_profile="fixture-v1",
            priority=priority,
        )
        evaluation = self.repository.submit_evaluation(candidate, request)
        return {"candidate": candidate, "evaluation": evaluation}

    @staticmethod
    def preparation(submission: dict, *, target_id: str = TARGET) -> dict:
        option = make_execution_option(
            simulation_definition_artifact_id="simulation-definition.fixture",
            simulation_definition_revision=REVISION,
            runnable_package_artifact_id="package.fixture-p1",
            runnable_package_revision=REVISION,
            target_id=target_id,
            processors=1,
            memory_bytes=4 * 1024**3,
            performance_class_id=PERFORMANCE_CLASS_ID,
        )
        profile = make_performance_profile(
            execution_option_id=option["option_id"],
            evidence_artifact_id="evidence.performance.fixture",
            evidence_revision=REVISION,
            sample_count=2,
            duration_p50_seconds=120,
            duration_p90_seconds=150,
            peak_rss_p90_bytes=2 * 1024**3,
            performance_class_id=PERFORMANCE_CLASS_ID,
        )
        return make_execution_preparation(
            evaluation_id=submission["evaluation"]["evaluation_id"],
            candidate_id=submission["candidate"]["candidate_id"],
            simulation_proxy="simulation-session-v1",
            numerical_profile="proxy-managed-v1",
            recovery_profile_revision=REVISION,
            command_timeout_seconds=600,
            max_solver_runs=1,
            max_wall_seconds=900,
            execution_option_set=make_execution_option_set([option]),
            performance_profile_snapshot=make_performance_profile_snapshot(
                policy_revision=REVISION,
                profiles=[profile],
            ),
        )

    def claim(
        self,
        submissions: list[dict],
        *,
        controller: str = CONTROLLER,
        window_limit: int,
        now: datetime = BASE_TIME,
        lease_seconds: int = 120,
    ) -> list[dict]:
        return self.repository.claim_preparation_slots(
            [item["evaluation"]["evaluation_id"] for item in submissions],
            controller_id=controller,
            window_limit=window_limit,
            lease_seconds=lease_seconds,
            now=now,
        )

    def prepare(
        self,
        submission: dict,
        *,
        controller: str = CONTROLLER,
        window_limit: int = 1,
        now: datetime = BASE_TIME,
        target_id: str = TARGET,
    ) -> dict:
        claim = self.claim(
            [submission],
            controller=controller,
            window_limit=window_limit,
            now=now,
        )[0]
        return self.repository.commit_preparation_claim(
            claim["claim_id"],
            controller,
            self.preparation(submission, target_id=target_id),
            now=now,
        )

    def lease(
        self,
        attempt: dict,
        *,
        now: datetime,
        license_sessions: int | None = None,
    ) -> dict:
        preparation = attempt["execution_preparation"]
        target_id = preparation["execution_option_set"]["options"][0]["target_id"]
        resources = {
            "schema_version": 1,
            "snapshot_kind": "resource-snapshot",
            "snapshot_revision": REVISION,
            "target_id": target_id,
            "status": "ready",
            "available_processors": 1,
            "available_memory_bytes": 8 * 1024**3,
            "default_request_memory_bytes": 4 * 1024**3,
            "observed_allocation_keys": [],
            "reasons": [],
            # This fixture represents a monitor-held, attested snapshot;
            # production fail-closed validation remains unchanged.
            "created_at": now.isoformat(),
            "lock_held": True,
            "target_is_idle": True,
        }
        decision = schedule(
            [
                {
                    "attempt_id": attempt["attempt_id"],
                    "execution_option_set": preparation["execution_option_set"],
                    "performance_profile_snapshot": preparation[
                        "performance_profile_snapshot"
                    ],
                }
            ],
            [],
            resources,
        )
        option = decision["selected_execution_option"]
        plan = materialize_session_plan(
            attempt_id=attempt["attempt_id"],
            preparation=preparation,
            selected_option=option,
        )
        suffix = attempt["attempt_id"].split(":")[-1][:12]
        allocation = make_resource_allocation(
            decision,
            session_ref=f"session-{suffix}",
            run_id="20260803-000000-001",
            remote_workspace_root="/remote/test-workspace",
            decision_artifact_id="evidence.scheduling.fixture",
            decision_artifact_path="decision.json",
        )
        leased = self.repository.claim_prepared_execution(
            attempt["attempt_id"],
            WORKER,
            300,
            preparation_id=preparation["preparation_id"],
            selected_option_id=option["option_id"],
            session_plan=plan,
            allocation=allocation,
            license_sessions=license_sessions,
            now=now,
        )
        self.assertIsNotNone(leased)
        return leased

    def test_license_gate_rejects_at_global_limit_without_partial_writes(self) -> None:
        first = self.lease(self.prepare(self.submit()), now=BASE_TIME)
        second = self.prepare(self.submit(), window_limit=2)

        def fact_counts() -> tuple[int, int, int]:
            with closing(sqlite3.connect(self.database)) as connection:
                return tuple(
                    connection.execute(
                        "SELECT COUNT(*), "
                        "SUM(execution_plan_json IS NOT NULL), "
                        "SUM(allocation_json IS NOT NULL) FROM attempts"
                    ).fetchone()
                )

        before = fact_counts()
        with self.assertRaisesRegex(RepositoryError, "license sessions exhausted"):
            self.lease(second, now=BASE_TIME, license_sessions=1)
        after = fact_counts()

        self.assertEqual(before, after)
        stored = self.repository.get_attempt(second["attempt_id"])
        self.assertEqual(stored["status"], "planned")
        self.assertIsNone(stored["selected_execution_option_id"])
        self.assertIsNone(stored["execution_plan"])
        self.assertIsNone(stored["allocation"])
        self.assertIsNone(stored["lease_owner"])
        self.assertEqual(
            {item["attempt_id"] for item in self.repository.list_active_allocations()},
            {first["attempt_id"]},
        )

    def test_license_gate_counts_active_allocations_on_other_targets(self) -> None:
        first = self.lease(
            self.prepare(self.submit(), target_id=OTHER_TARGET), now=BASE_TIME
        )
        second = self.prepare(self.submit(), window_limit=2, target_id=TARGET)

        with self.assertRaisesRegex(RepositoryError, "license sessions exhausted"):
            self.lease(second, now=BASE_TIME, license_sessions=1)

        stored = self.repository.get_attempt(second["attempt_id"])
        self.assertEqual(stored["status"], "planned")
        self.assertIsNone(stored["allocation"])
        self.assertEqual(
            self.repository.list_active_allocations()[0]["attempt_id"],
            first["attempt_id"],
        )

    def test_license_gate_allows_claim_when_capacity_remains(self) -> None:
        first = self.lease(self.prepare(self.submit()), now=BASE_TIME)
        second = self.lease(
            self.prepare(self.submit(), window_limit=2),
            now=BASE_TIME,
            license_sessions=2,
        )

        self.assertEqual(
            {item["attempt_id"] for item in self.repository.list_active_allocations()},
            {first["attempt_id"], second["attempt_id"]},
        )

    def test_list_active_allocations_can_return_all_targets(self) -> None:
        first = self.submit()
        first_attempt = self.lease(self.prepare(first), now=BASE_TIME)
        second = self.submit()
        second_attempt = self.lease(
            self.prepare(second, window_limit=2, target_id=OTHER_TARGET),
            now=BASE_TIME,
        )

        all_allocations = self.repository.list_active_allocations()
        self.assertEqual(
            {item["attempt_id"] for item in all_allocations},
            {first_attempt["attempt_id"], second_attempt["attempt_id"]},
        )
        self.assertEqual(
            [item["attempt_id"] for item in self.repository.list_active_allocations(TARGET)],
            [first_attempt["attempt_id"]],
        )
        self.assertEqual(
            [item["attempt_id"] for item in self.repository.list_active_allocations(OTHER_TARGET)],
            [second_attempt["attempt_id"]],
        )

    def test_schema_v9_and_full_prepared_window_metadata(self) -> None:
        self.assertEqual(MIN_TERMINAL_COMPLETION_SAMPLES, 5)
        submissions = [self.submit(priority=value) for value in ("low", "normal", "high")]
        claims = self.claim(submissions, window_limit=3)
        for submission, claim in zip(submissions, claims):
            self.repository.commit_preparation_claim(
                claim["claim_id"],
                CONTROLLER,
                self.preparation(submission),
                now=BASE_TIME,
            )

        prepared = self.repository.list_prepared_scheduling_candidates()
        self.assertEqual(len(prepared), 3)
        self.assertEqual(
            self.repository.preparation_window_occupancy(now=BASE_TIME), 3
        )
        self.assertEqual(len(self.repository.list_prepared_scheduling_candidates(2)), 2)
        self.assertEqual(
            {item["priority"] for item in prepared}, {"low", "normal", "high"}
        )
        self.assertTrue(all(item["queued_since"] for item in prepared))
        self.assertEqual(self.repository.list_queued_evaluations(now=BASE_TIME), [])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0],
                SCHEMA_VERSION,
            )
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'preparation_claims'"
                ).fetchone()
            )
            for table in ("attempt_feedback", "task_shape_stats"):
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (table,),
                    ).fetchone()
                )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(attempts)")}
            self.assertTrue({"feedback_json", "feedback_recorded_at"} <= columns)

    def test_schema_v10_database_adds_study_profile_without_data_loss(self) -> None:
        legacy = Path(self.temp.name) / "legacy-v10.sqlite3"
        with closing(sqlite3.connect(legacy)) as connection:
            connection.executescript(f"""
                CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
                INSERT INTO schema_migrations VALUES (10, '2026-08-02T00:00:00+00:00');
                CREATE TABLE problem_definitions (problem_id TEXT NOT NULL, revision TEXT NOT NULL,
                    definition_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(problem_id, revision));
                INSERT INTO problem_definitions VALUES ('legacy-problem', '{REVISION}', '{{}}', 'old');
                CREATE TABLE studies (study_id TEXT PRIMARY KEY, problem_id TEXT NOT NULL,
                    problem_revision TEXT NOT NULL, created_at TEXT NOT NULL, metadata_json TEXT NOT NULL,
                    algorithm_run_id TEXT, artifact_refs_json TEXT NOT NULL);
                INSERT INTO studies VALUES ('study:legacy', 'legacy-problem', '{REVISION}', 'old', '{{"kept":true}}', NULL, '[]');
            """)
            connection.commit()
        repository = SQLiteEvaluationRepository(legacy)
        study = repository.get_study("study:legacy")
        self.assertEqual(study["automation_profile"], "assisted")
        self.assertEqual(study["metadata"], {"kept": True})
        with closing(sqlite3.connect(legacy)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(studies)")}
            self.assertIn("automation_profile", columns)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM studies").fetchone()[0], 1)

    def test_schema_v8_database_migrates_to_current_schema(self) -> None:
        legacy = Path(self.temp.name) / "legacy-v8.sqlite3"
        with closing(sqlite3.connect(legacy)) as connection:
            connection.executescript(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                INSERT INTO schema_migrations(version, applied_at)
                VALUES (8, '2026-08-02T00:00:00+00:00');
                """
            )
            connection.commit()

        SQLiteEvaluationRepository(legacy)
        with closing(sqlite3.connect(legacy)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT MAX(version) FROM schema_migrations"
                ).fetchone()[0],
                SCHEMA_VERSION,
            )
            for table in ("attempt_feedback", "task_shape_stats"):
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (table,),
                    ).fetchone()
                )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(attempts)")}
            self.assertTrue({"feedback_json", "feedback_recorded_at"} <= columns)

    def test_concurrent_controllers_never_exceed_window_limit(self) -> None:
        submissions = [self.submit() for _ in range(8)]
        evaluation_ids = [item["evaluation"]["evaluation_id"] for item in submissions]
        barrier = Barrier(2)

        def compete(controller: str) -> list[dict]:
            repository = SQLiteEvaluationRepository(self.database)
            barrier.wait()
            return repository.claim_preparation_slots(
                evaluation_ids,
                controller_id=controller,
                window_limit=3,
                lease_seconds=120,
                now=BASE_TIME,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compete, ("controller:a", "controller:b")))

        claims = [claim for result in results for claim in result]
        self.assertEqual(len(claims), 3)
        self.assertEqual(len({claim["evaluation_id"] for claim in claims}), 3)
        self.assertEqual(
            self.repository.preparation_window_occupancy(now=BASE_TIME), 3
        )
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM preparation_claims").fetchone()[0],
                3,
            )

    def test_expired_and_released_claims_return_to_the_queue(self) -> None:
        submission = self.submit()
        first = self.claim(
            [submission], window_limit=1, lease_seconds=1, now=BASE_TIME
        )[0]
        self.assertEqual(self.repository.list_queued_evaluations(now=BASE_TIME), [])

        later = BASE_TIME + timedelta(seconds=2)
        self.assertEqual(self.repository.preparation_window_occupancy(now=later), 0)
        queued = self.repository.list_queued_evaluations(now=later)
        self.assertEqual([item["evaluation_id"] for item in queued], [first["evaluation_id"]])
        second = self.claim(
            [submission],
            controller="controller:replacement",
            window_limit=1,
            now=later,
        )[0]
        self.assertNotEqual(second["claim_id"], first["claim_id"])
        self.assertFalse(
            self.repository.release_preparation_claim(
                first["claim_id"], CONTROLLER, reason="already expired", now=later
            )
        )
        self.assertTrue(
            self.repository.release_preparation_claim(
                second["claim_id"],
                "controller:replacement",
                reason="policy deferred",
                now=later,
            )
        )
        self.assertFalse(
            self.repository.release_preparation_claim(
                second["claim_id"],
                "controller:replacement",
                reason="policy deferred",
                now=later,
            )
        )
        self.assertEqual(len(self.repository.list_queued_evaluations(now=later)), 1)

    def test_claim_commit_is_idempotent_and_counts_as_window_occupancy(self) -> None:
        first, second = self.submit(), self.submit()
        claim = self.claim([first], window_limit=1)[0]
        preparation = self.preparation(first)
        attempt = self.repository.commit_preparation_claim(
            claim["claim_id"], CONTROLLER, preparation, now=BASE_TIME
        )
        replay = self.repository.commit_preparation_claim(
            claim["claim_id"], CONTROLLER, preparation, now=BASE_TIME
        )
        self.assertEqual(replay["attempt_id"], attempt["attempt_id"])
        self.assertEqual(self.claim([second], window_limit=1), [])

    def test_completed_and_failed_attempts_each_release_one_window_slot(self) -> None:
        first, second, third = self.submit(), self.submit(), self.submit()
        first_attempt = self.prepare(first, now=BASE_TIME)
        first_attempt = self.lease(first_attempt, now=BASE_TIME + timedelta(seconds=1))
        self.repository.confirm_attempt_start(
            first_attempt["attempt_id"], WORKER, now=BASE_TIME + timedelta(seconds=1)
        )
        self.repository.begin_collection(
            first_attempt["attempt_id"], WORKER, now=BASE_TIME + timedelta(seconds=2)
        )
        self.repository.complete_attempt(
            first_attempt["attempt_id"],
            WORKER,
            ["evidence.fixture.completed"],
            now=BASE_TIME + timedelta(seconds=3),
            _validated_session_result=True,
        )
        self.assertEqual(
            self.repository.preparation_window_occupancy(
                now=BASE_TIME + timedelta(seconds=3)
            ),
            0,
        )

        second_claim = self.claim(
            [second], window_limit=1, now=BASE_TIME + timedelta(seconds=4)
        )[0]
        second_attempt = self.repository.commit_preparation_claim(
            second_claim["claim_id"],
            CONTROLLER,
            self.preparation(second),
            now=BASE_TIME + timedelta(seconds=4),
        )
        self.lease(second_attempt, now=BASE_TIME + timedelta(seconds=5))
        self.repository.fail_attempt(
            second_attempt["attempt_id"],
            WORKER,
            "fixture-failure",
            now=BASE_TIME + timedelta(seconds=6),
        )
        self.assertEqual(
            self.repository.preparation_window_occupancy(
                now=BASE_TIME + timedelta(seconds=6)
            ),
            0,
        )

        third_claims = self.claim(
            [third], window_limit=1, now=BASE_TIME + timedelta(seconds=7)
        )
        self.assertEqual(len(third_claims), 1)

    def test_revoked_unstarted_preparation_can_be_retired_without_cancelling_evaluation(
        self,
    ) -> None:
        first, second = self.submit(), self.submit()
        attempt = self.prepare(first)
        self.assertEqual(self.claim([second], window_limit=1), [])

        retired = self.repository.retire_unstarted_preparation(
            attempt["attempt_id"],
            attempt["execution_preparation_id"],
            reason="preparation authorization revoked",
            now=BASE_TIME + timedelta(seconds=1),
        )
        replay = self.repository.retire_unstarted_preparation(
            attempt["attempt_id"],
            attempt["execution_preparation_id"],
            reason="preparation authorization revoked",
            now=BASE_TIME + timedelta(seconds=1),
        )
        self.assertEqual(retired["status"], "cancelled")
        self.assertEqual(
            self.repository.preparation_window_occupancy(
                now=BASE_TIME + timedelta(seconds=1)
            ),
            0,
        )
        self.assertEqual(replay["attempt_id"], retired["attempt_id"])
        self.assertIsNotNone(retired["execution_preparation"])
        self.assertEqual(
            self.repository.get_evaluation(first["evaluation"]["evaluation_id"])[
                "status"
            ],
            "queued",
        )
        replacement = self.claim(
            [second, first], window_limit=1, now=BASE_TIME + timedelta(seconds=2)
        )
        self.assertEqual(
            [item["evaluation_id"] for item in replacement],
            [second["evaluation"]["evaluation_id"]],
        )

    def _reconciling_attempt(self, *, now: datetime, submission: dict | None = None) -> dict:
        submission = self.submit() if submission is None else submission
        prepared = self.prepare(submission, now=BASE_TIME, window_limit=2)
        leased = self.lease(prepared, now=now)
        return self.repository.mark_attempt_reconciling(
            leased["attempt_id"], WORKER, ["artifact:fixture"],
            reason="fixture-reconciliation", now=now,
        )

    def test_wall_proof_real_prepared_path_releases_lost(self) -> None:
        attempt = self._reconciling_attempt(now=BASE_TIME)
        middleware = EvaluationMiddleware(self.repository)
        results = middleware.auto_release_wall_budget(
            now=BASE_TIME + timedelta(seconds=900 + 600 + 600 + 1)
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "released")
        self.assertEqual(self.repository.get_attempt(attempt["attempt_id"])["status"], "lost")
        self.assertNotIn(
            attempt["attempt_id"],
            {item["attempt_id"] for item in self.repository.list_active_allocations()},
        )
        event = [
            item for item in self.repository.state_events(attempt["attempt_id"])
            if item["event_type"] == "AttemptLost"
        ][-1]
        self.assertEqual(event["payload"]["source"], "auto:wall-proof")
        self.assertEqual(event["payload"]["proof_seconds"], 2100)
        self.assertEqual(event["payload"]["claimed_at"], results[0]["claimed_at"])
        self.assertEqual(event["payload"]["age_seconds"], results[0]["age_seconds"])

    def test_wall_proof_boundary_and_budget_unavailable(self) -> None:
        boundary = self._reconciling_attempt(now=BASE_TIME)
        middleware = EvaluationMiddleware(self.repository)
        self.assertEqual(
            middleware.auto_release_wall_budget(now=BASE_TIME + timedelta(seconds=2100)),
            [],
        )
        self.assertEqual(self.repository.get_attempt(boundary["attempt_id"])["status"], "reconciling")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE attempts SET execution_preparation_json = ?, execution_plan_json = NULL WHERE attempt_id = ?",
                (json.dumps({"budget": {"max_wall_seconds": 900}}), boundary["attempt_id"]),
            )
            connection.commit()
        skipped = middleware.auto_release_wall_budget(now=BASE_TIME + timedelta(seconds=3000))
        self.assertEqual(skipped[0]["status"], "skipped")
        self.assertEqual(skipped[0]["reason"], "budget-unavailable")
        self.assertEqual(self.repository.get_attempt(boundary["attempt_id"])["status"], "reconciling")

    def _timeout_attempt(self, *, now: datetime = BASE_TIME, submission: dict | None = None) -> dict:
        attempt = self._reconciling_attempt(now=now, submission=submission)
        return self.repository.fail_attempt(
            attempt["attempt_id"], WORKER, "timeout", now=now
        )

    def test_multiple_studies_use_the_most_conservative_profile(self) -> None:
        submission = self.submit()
        self.repository.create_study(study_id="study:auto", problem_id=self.problem["problem_id"], problem_revision=self.problem["revision"], automation_profile="autonomous")
        self.repository.create_study(study_id="study:manual", problem_id=self.problem["problem_id"], problem_revision=self.problem["revision"], automation_profile="manual")
        self.repository.associate_study_evaluation("study:auto", submission["evaluation"]["evaluation_id"])
        self.repository.associate_study_evaluation("study:manual", submission["evaluation"]["evaluation_id"])
        attempt = self._timeout_attempt(submission=submission)
        policy = json.loads(json.dumps(DEFAULT_AUTOMATION_POLICY))
        policy["default_profile"] = "autonomous"
        result = self.repository.auto_requeue_recovering(now=BASE_TIME, automation_policy=policy)
        self.assertEqual(result[0]["action"], "held")
        self.assertEqual(result[0]["rule"], "tier0-first-timeout-held")
        self.assertEqual(self.repository.get_evaluation(attempt["evaluation_id"])["status"], "recovering")

    def test_tier_one_timeout_payload_contains_statistics_at_twenty_samples(self) -> None:
        attempt = self._timeout_attempt()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone()
            shape = self.repository._attempt_shape(row)
            assert shape is not None
            connection.execute("INSERT OR REPLACE INTO task_shape_stats(task_class_key,target_id,profile_revision,processors,sample_count,success_count,failure_count,wall_samples,wall_mean_seconds,wall_m2_seconds,cpu_samples,cpu_mean_seconds,cpu_m2_seconds,busy_samples,busy_mean_seconds,busy_m2_seconds,rss_samples,rss_mean_bytes,rss_m2_bytes,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*shape,20,20,0,20,100.0,0.0,0,None,None,0,None,None,0,None,None,"now"))
            connection.commit()
        result = self.repository.auto_requeue_recovering(now=BASE_TIME)
        self.assertEqual(result[0]["tier"], 1)
        self.assertEqual(result[0]["input"]["statistics"]["sample_count"], 20)
        self.assertEqual(result[0]["input"]["statistics"]["mean"], 100.0)

    def test_timeout_triage_first_rerun_and_held_profile(self) -> None:
        first = self._timeout_attempt()
        triage = self.repository.auto_requeue_recovering(now=BASE_TIME)
        self.assertEqual(triage[0]["action"], "requeued")
        self.assertEqual(triage[0]["rule"], "tier0-first-timeout-rerun")
        self.assertEqual(self.repository.get_evaluation(first["evaluation_id"])["status"], "queued")
        payload = [item for item in self.repository.state_events(first["evaluation_id"]) if item["event_type"] == "RecoveryPlanned"][-1]["payload"]
        self.assertEqual(payload["source"], "auto:requeue")
        submission = {"candidate": self.repository.get_candidate(self.repository.get_evaluation(first["evaluation_id"])["candidate_id"]), "evaluation": self.repository.get_evaluation(first["evaluation_id"])}
        second = self._timeout_attempt(now=BASE_TIME + timedelta(seconds=1), submission=submission)
        held = self.repository.auto_requeue_recovering(now=BASE_TIME + timedelta(seconds=1))
        self.assertEqual(held[0]["action"], "held")
        self.assertIn("evidence", held[0])
        self.assertEqual(self.repository.get_evaluation(second["evaluation_id"])["status"], "recovering")

    def test_timeout_profile_skip_marks_unresolved_with_deterministic_reason(self) -> None:
        attempt = self._timeout_attempt()
        policy = json.loads(json.dumps(DEFAULT_AUTOMATION_POLICY))
        policy["default_profile"] = "autonomous"
        result = self.repository.auto_requeue_recovering(now=BASE_TIME, automation_policy=policy)
        self.assertEqual(result[0]["action"], "requeued")
        # A second timeout is deterministic and the autonomous profile marks it.
        submission = {"candidate": self.repository.get_candidate(self.repository.get_evaluation(attempt["evaluation_id"])["candidate_id"]), "evaluation": self.repository.get_evaluation(attempt["evaluation_id"])}
        self._timeout_attempt(now=BASE_TIME + timedelta(seconds=1), submission=submission)
        result = self.repository.auto_requeue_recovering(now=BASE_TIME + timedelta(seconds=1), automation_policy=policy)
        self.assertEqual(result[0]["action"], "marked-unresolved")
        self.assertIn("deterministic-timeout", self.repository.state_events(attempt["evaluation_id"])[-1]["payload"].get("reason", ""))

    def test_whitelist_requeues_twice_but_operator_force_lost_never_does(self) -> None:
        attempt = self._reconciling_attempt(now=BASE_TIME)
        self.repository.reconcile_attempt(attempt["attempt_id"], WORKER, attempt["allocation"]["session_ref"], "absent", 300, now=BASE_TIME + timedelta(seconds=1))
        middleware = EvaluationMiddleware(self.repository)
        self.assertEqual(middleware.auto_requeue_recovering(now=BASE_TIME + timedelta(seconds=2))[0]["action"], "requeued")
        submission = {"candidate": self.repository.get_candidate(self.repository.get_evaluation(attempt["evaluation_id"])["candidate_id"]), "evaluation": self.repository.get_evaluation(attempt["evaluation_id"])}
        second = self.lease(self.prepare(submission, now=BASE_TIME + timedelta(seconds=3), window_limit=2), now=BASE_TIME + timedelta(seconds=3))
        self.repository.mark_attempt_reconciling(second["attempt_id"], WORKER, ["artifact:fixture"], reason="fixture", now=BASE_TIME + timedelta(seconds=4))
        self.repository.reconcile_attempt(second["attempt_id"], WORKER, second["allocation"]["session_ref"], "absent", 300, now=BASE_TIME + timedelta(seconds=5))
        self.assertEqual(middleware.auto_requeue_recovering(now=BASE_TIME + timedelta(seconds=6))[0]["automatic_requeue_count"], 2)
        other = self._reconciling_attempt(now=BASE_TIME + timedelta(seconds=10))
        self.repository.force_lost_attempt(other["attempt_id"], "operator-force-lost", now=BASE_TIME + timedelta(seconds=11))
        self.assertEqual(middleware.auto_requeue_recovering(now=BASE_TIME + timedelta(seconds=12)), [])

    def test_budget_proposals_require_thirty_samples_and_round_up_to_sixty(self) -> None:
        row = ("shape-fixture", TARGET, REVISION, 1, 29, 29, 0, 29, 100.0, 0.0, 0, None, None, 0, None, None, 0, None, None, "now")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("INSERT INTO task_shape_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            connection.commit()
        self.assertEqual(self.repository.budget_proposals(), [])
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE task_shape_stats SET sample_count=30, wall_m2_seconds=100", )
            connection.commit()
        proposals = self.repository.budget_proposals()
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["suggested_max_wall_seconds"] % 60, 0)
        self.assertEqual(proposals[0]["mean"], 100.0)

    def test_auto_requeue_whitelist_and_operator_lost_are_distinct(self) -> None:
        attempt = self._reconciling_attempt(now=BASE_TIME)
        self.repository.reconcile_attempt(
            attempt["attempt_id"], WORKER, attempt["allocation"]["session_ref"],
            "absent", 300, now=BASE_TIME + timedelta(seconds=1),
        )
        middleware = EvaluationMiddleware(self.repository)
        # force-lost transitions the evaluation to recovering; only the whitelist is automatic.
        automatic = middleware.auto_requeue_recovering(now=BASE_TIME + timedelta(seconds=2))
        self.assertEqual(automatic[0]["status"], "queued")
        self.assertEqual(automatic[0]["automatic_requeue_count"], 1)
        event = [e for e in self.repository.state_events(attempt["evaluation_id"]) if e["event_type"] == "RecoveryPlanned"][-1]
        self.assertEqual(event["payload"]["source"], "auto:requeue")

        other = self._reconciling_attempt(now=BASE_TIME + timedelta(seconds=10))
        self.repository.force_lost_attempt(other["attempt_id"], "operator-force-lost", now=BASE_TIME + timedelta(seconds=11))
        self.assertEqual(middleware.auto_requeue_recovering(now=BASE_TIME + timedelta(seconds=12)), [])

    def test_stale_reconciling_attempts_filters_age_and_status(self) -> None:
        stale = self._reconciling_attempt(now=BASE_TIME)
        fresh = self._reconciling_attempt(now=BASE_TIME + timedelta(seconds=3599))
        result = self.repository.list_stale_reconciling_attempts(
            3600, now=BASE_TIME + timedelta(seconds=3601)
        )
        self.assertEqual([item["attempt_id"] for item in result], [stale["attempt_id"]])
        item = result[0]
        self.assertEqual(item["evaluation_id"], stale["evaluation_id"])
        self.assertEqual(item["target_id"], TARGET)
        self.assertEqual(item["processors"], 1)
        self.assertEqual(item["memory_bytes"], 4 * 1024**3)
        self.assertEqual(item["age_seconds"], 3601.0)
        self.assertNotIn(fresh["attempt_id"], {entry["attempt_id"] for entry in result})

        with self.assertRaises(RepositoryError):
            self.repository.list_stale_reconciling_attempts(-1, now=BASE_TIME)
        with self.assertRaises(RepositoryError):
            self.repository.list_stale_reconciling_attempts(True, now=BASE_TIME)  # type: ignore[arg-type]

    def test_stale_reconciling_uses_each_attempts_latest_event(self) -> None:
        first = self._reconciling_attempt(now=BASE_TIME)
        second = self._reconciling_attempt(now=BASE_TIME + timedelta(seconds=100))
        result = self.repository.list_stale_reconciling_attempts(
            0, now=BASE_TIME + timedelta(seconds=200)
        )
        self.assertEqual(
            {entry["attempt_id"] for entry in result},
            {first["attempt_id"], second["attempt_id"]},
        )
        by_id = {entry["attempt_id"]: entry for entry in result}
        self.assertEqual(by_id[first["attempt_id"]]["age_seconds"], 200.0)
        self.assertEqual(by_id[second["attempt_id"]]["age_seconds"], 100.0)

    def test_force_lost_releases_allocation_and_recovers_evaluation(self) -> None:
        attempt = self._reconciling_attempt(now=BASE_TIME)
        forced = self.repository.force_lost_attempt(
            attempt["attempt_id"], "operator confirmed remote loss", now=BASE_TIME + timedelta(seconds=1)
        )
        self.assertEqual(forced["new_status"], "lost")
        self.assertNotIn(
            attempt["attempt_id"],
            {item["attempt_id"] for item in self.repository.list_active_allocations()},
        )
        events = self.repository.state_events(attempt["attempt_id"])
        lost = next(event for event in events if event["event_type"] == "AttemptLost")
        self.assertEqual(lost["payload"]["reason"], "operator confirmed remote loss")
        self.assertTrue(datetime.fromisoformat(lost["created_at"]).tzinfo)
        self.assertEqual(forced["evaluation"]["status"], "recovering")
        self.assertEqual(
            self.repository.list_stale_reconciling_attempts(0, now=BASE_TIME + timedelta(seconds=2)),
            [],
        )

    def test_force_lost_rejects_invalid_requests(self) -> None:
        with self.assertRaises(RepositoryError):
            self.repository.force_lost_attempt("attempt:missing", "reason", now=BASE_TIME)
        with self.assertRaises(RepositoryError):
            self.repository.force_lost_attempt("attempt:missing", "", now=BASE_TIME)
        attempt = self._reconciling_attempt(now=BASE_TIME)
        self.repository.force_lost_attempt(attempt["attempt_id"], "done", now=BASE_TIME)
        with self.assertRaises(RepositoryError):
            self.repository.force_lost_attempt(attempt["attempt_id"], "again", now=BASE_TIME)

    def test_commit_rejects_a_claim_for_another_evaluation(self) -> None:
        first, second = self.submit(), self.submit()
        claim = self.claim([first], window_limit=1)[0]
        with self.assertRaisesRegex(RepositoryError, "different Evaluation"):
            self.repository.commit_preparation_claim(
                claim["claim_id"],
                CONTROLLER,
                self.preparation(second),
                now=BASE_TIME,
            )
        self.assertEqual(
            self.claim([first], window_limit=1)[0]["claim_id"], claim["claim_id"]
        )
    def _sqlite_index(self) -> tuple[str, int]:
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(
                "SELECT sql, rootpage FROM sqlite_master "
                "WHERE type='index' AND name='idx_attempts_active_preparation'"
            ).fetchone()

    def test_v11_database_is_upgraded_to_v12_index_and_heartbeat_column(self) -> None:
        # Downgrade only metadata and the index; this exercises repository startup
        # rather than manually simulating migration code.
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version = 12")
            connection.execute("DROP INDEX idx_attempts_active_preparation")
            connection.execute(
                "CREATE UNIQUE INDEX idx_attempts_active_preparation "
                "ON attempts(execution_preparation_id) "
                "WHERE execution_preparation_id IS NOT NULL "
                "AND status IN ('planned', 'leased', 'running', 'collecting', 'reconciling')"
            )
            connection.commit()
        SQLiteEvaluationRepository(self.database)
        sql, _ = self._sqlite_index()
        self.assertIn("starting", sql.lower())
        self.assertIn("unconfirmed", sql.lower())
        with closing(sqlite3.connect(self.database)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(attempts)")
            }
            versions = {
                row[0] for row in connection.execute("SELECT version FROM schema_migrations")
            }
        self.assertIn("last_heartbeat_at", columns)
        self.assertIn(13, versions)

    def test_v12_initialization_is_idempotent_without_rebuilding_index(self) -> None:
        before_sql, before_rootpage = self._sqlite_index()
        SQLiteEvaluationRepository(self.database)
        after_sql, after_rootpage = self._sqlite_index()
        self.assertEqual(before_sql, after_sql)
        self.assertEqual(before_rootpage, after_rootpage)
        self.assertIn("starting", after_sql.lower())
        self.assertIn("unconfirmed", after_sql.lower())

    def _duplicate_attempt_with_status(self, status: str, suffix: str) -> tuple[str, str]:
        attempt = self.prepare(self.submit(), window_limit=3)
        duplicate = f"attempt:duplicate-{suffix}"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE attempts SET status=? WHERE attempt_id=?",
                (status, attempt["attempt_id"]),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO attempts "
                    "(attempt_id, evaluation_id, attempt_number, simulation_adapter, "
                    "numerical_profile, status, artifact_ids_json, "
                    "execution_preparation_id, created_at, updated_at) "
                    "SELECT ?, evaluation_id, attempt_number + 100, simulation_adapter, "
                    "numerical_profile, ?, artifact_ids_json, execution_preparation_id, "
                    "created_at, updated_at "
                    "FROM attempts WHERE attempt_id=?",
                    (duplicate, status, attempt["attempt_id"]),
                )
            connection.rollback()
        return attempt["attempt_id"], duplicate

    def test_starting_and_unconfirmed_are_unique_active_preparations(self) -> None:
        for status in ("starting", "unconfirmed"):
            self._duplicate_attempt_with_status(status, status)

    def test_terminal_attempt_does_not_conflict_on_preparation(self) -> None:
        attempt = self.prepare(self.submit())
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE attempts SET status='starting' WHERE attempt_id=?",
                (attempt["attempt_id"],),
            )
            connection.execute(
                "INSERT INTO attempts "
                "(attempt_id, evaluation_id, attempt_number, simulation_adapter, "
                "numerical_profile, status, artifact_ids_json, "
                "execution_preparation_id, created_at, updated_at) "
                "SELECT ?, evaluation_id, attempt_number + 100, simulation_adapter, "
                "numerical_profile, 'failed', artifact_ids_json, "
                "execution_preparation_id, created_at, updated_at "
                "FROM attempts WHERE attempt_id=?",
                ("attempt:terminal-duplicate", attempt["attempt_id"]),
            )
            connection.commit()

    def test_new_states_are_in_active_allocation_projection(self) -> None:
        for status in ("starting", "unconfirmed"):
            attempt = self.lease(
                self.prepare(self.submit(), window_limit=3), now=BASE_TIME
            )
            with closing(sqlite3.connect(self.database)) as connection:
                connection.execute(
                    "UPDATE attempts SET status=? WHERE attempt_id=?",
                    (status, attempt["attempt_id"]),
                )
                connection.commit()
            ids = {row["attempt_id"] for row in self.repository.list_active_allocations()}
            self.assertIn(attempt["attempt_id"], ids)

        planned = self.lease(
            self.prepare(self.submit(), window_limit=4), now=BASE_TIME
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE attempts SET status='planned' WHERE attempt_id=?",
                (planned["attempt_id"],),
            )
            connection.commit()
        self.assertNotIn(
            planned["attempt_id"],
            {row["attempt_id"] for row in self.repository.list_active_allocations()},
        )

    def test_attempt_state_constant_invariants(self) -> None:
        self.assertTrue({"starting", "unconfirmed"} <= ATTEMPT_STATES)
        self.assertTrue({"starting", "unconfirmed"} <= ACTIVE_ATTEMPT_STATES)
        self.assertTrue({"starting", "unconfirmed"} <= CAPACITY_HOLDING_ATTEMPT_STATES)
        self.assertIn("starting", HEARTBEATABLE_ATTEMPT_STATES)
        # An unconfirmed attempt has no confirmed owner, so cannot heartbeat.
        self.assertNotIn("unconfirmed", HEARTBEATABLE_ATTEMPT_STATES)
        self.assertNotIn("planned", CAPACITY_HOLDING_ATTEMPT_STATES)
        self.assertNotIn("reconciling", HEARTBEATABLE_ATTEMPT_STATES)
        outputs = [attempt_states_sql(ATTEMPT_STATES) for _ in range(3)]
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1], outputs[2])
        self.assertEqual(
            outputs[0],
            ", ".join(f"'{state}'" for state in _ATTEMPT_STATE_SQL_ORDER),
        )

    def test_heartbeat_writes_last_heartbeat_at(self) -> None:
        attempt = self.lease(self.prepare(self.submit()), now=BASE_TIME)
        renewed_at = BASE_TIME + timedelta(seconds=30)
        renewed = self.repository.heartbeat(
            attempt["attempt_id"], WORKER, 300, now=renewed_at
        )
        self.assertEqual(
            datetime.fromisoformat(renewed["last_heartbeat_at"]), renewed_at
        )
        self.assertGreater(
            datetime.fromisoformat(renewed["last_heartbeat_at"]),
            datetime.fromisoformat(attempt["last_heartbeat_at"])
            if attempt["last_heartbeat_at"] is not None
            else BASE_TIME - timedelta(seconds=1),
        )

    def _txn_counter(self):
        calls = []
        original = self.repository._transaction

        def counted(*args, **kwargs):
            calls.append(True)
            return original(*args, **kwargs)

        return calls, counted

    def test_recovery_probes_do_not_open_write_transactions_when_idle(self) -> None:
        middleware = EvaluationMiddleware(self.repository)
        worker = mock.Mock()
        dispatcher = SessionLifecycleDispatcher(
            middleware, mock.Mock(), worker,
            dispatcher_id="dispatcher:probe", lease_seconds=30,
        )
        calls, counted = self._txn_counter()
        with mock.patch.object(self.repository, "_transaction", counted):
            self.assertIsNone(dispatcher.recover_once(now=BASE_TIME))
        self.assertEqual(len(calls), 0)

    def test_each_idle_recovery_step_skips_write_transaction(self) -> None:
        middleware = EvaluationMiddleware(self.repository)
        cases = [
            lambda: middleware.expire_leases(now=BASE_TIME),
            lambda: middleware.auto_release_wall_budget(now=BASE_TIME),
            lambda: middleware.auto_requeue_recovering(now=BASE_TIME),
            lambda: middleware.lease_next_reconciliation("observer:idle", 30, now=BASE_TIME),
        ]
        for operation in cases:
            calls, counted = self._txn_counter()
            with mock.patch.object(self.repository, "_transaction", counted):
                self.assertIn(operation(), ([], None))
            self.assertEqual(len(calls), 0)

    def test_expired_leased_attempt_becomes_lost(self) -> None:
        attempt = self.lease(self.prepare(self.submit()), now=BASE_TIME)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE attempts SET status='leased' WHERE attempt_id=?",
                (attempt["attempt_id"],),
            )
            connection.commit()
        later = BASE_TIME + timedelta(seconds=301)
        expired = self.repository.expire_leases(now=later)
        stored = self.repository.get_attempt(attempt["attempt_id"])
        self.assertEqual(stored["status"], "lost")
        self.assertEqual(stored["failure_class"], "worker-lease-expired-before-start")
        self.assertTrue(any(e["event_type"] == "AttemptLost" for e in self.repository.state_events(attempt["attempt_id"])))

    def test_expired_running_attempt_becomes_reconciling(self) -> None:
        attempt = self.lease(self.prepare(self.submit()), now=BASE_TIME)
        self.repository.confirm_attempt_start(attempt["attempt_id"], WORKER, now=BASE_TIME)
        later = BASE_TIME + timedelta(seconds=301)
        self.assertEqual(self.repository.expire_leases(now=later), [attempt["attempt_id"]])
        stored = self.repository.get_attempt(attempt["attempt_id"])
        self.assertEqual(stored["status"], "reconciling")
        self.assertIsNone(stored["lease_owner"])
        self.assertTrue(any(e["event_type"] == "AttemptReconciliationRequired" for e in self.repository.state_events(attempt["attempt_id"])))

    def test_recovery_steps_process_real_work(self) -> None:
        wall = self._reconciling_attempt(now=BASE_TIME)
        middleware = EvaluationMiddleware(self.repository)
        released = middleware.auto_release_wall_budget(
            now=BASE_TIME + timedelta(seconds=2101)
        )
        self.assertEqual(released[0]["status"], "released")
        self.assertEqual(self.repository.get_attempt(wall["attempt_id"])["status"], "lost")
        self.assertTrue(
            any(
                event["event_type"] == "AttemptLost"
                and event["payload"].get("source") == "auto:wall-proof"
                for event in self.repository.state_events(wall["attempt_id"])
            )
        )

        recovering = self._timeout_attempt()
        triage = middleware.auto_requeue_recovering(now=BASE_TIME)
        matching = [
            item for item in triage
            if item.get("evaluation_id") == recovering["evaluation_id"]
        ]
        self.assertTrue(matching)
        self.assertEqual(matching[0]["action"], "requeued")
        self.assertEqual(
            self.repository.get_evaluation(recovering["evaluation_id"])["status"],
            "queued",
        )
        self.assertTrue(
            any(
                event["event_type"] == "RecoveryPlanned"
                and event["payload"].get("source") == "auto:requeue"
                for event in self.repository.state_events(recovering["evaluation_id"])
            )
        )
        candidate = self._reconciling_attempt(now=BASE_TIME + timedelta(seconds=1))
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE attempts SET lease_owner=NULL, lease_expires_at=NULL WHERE attempt_id=?",
                (candidate["attempt_id"],),
            )
            connection.commit()
        leased = middleware.lease_next_reconciliation("observer:work", 30, now=BASE_TIME)
        self.assertEqual(leased["attempt_id"], candidate["attempt_id"])
        self.assertEqual(leased["lease_owner"], "observer:work")
        self.assertTrue(
            any(
                event["event_type"] == "AttemptReconciliationLeased"
                for event in self.repository.state_events(candidate["attempt_id"])
            )
        )
    def test_recovery_tolerates_candidate_stolen_after_probe(self) -> None:
        middleware = EvaluationMiddleware(self.repository)
        worker = mock.Mock()
        dispatcher = SessionLifecycleDispatcher(
            middleware, mock.Mock(), worker,
            dispatcher_id="dispatcher:race", lease_seconds=30,
        )
        with mock.patch.object(middleware, "has_reconciliation_candidate", return_value=True), \
             mock.patch.object(middleware, "lease_next_reconciliation", return_value=None):
            self.assertIsNone(dispatcher.recover_once(now=BASE_TIME))

    def test_recovery_falls_back_for_middleware_without_probe_methods(self) -> None:
        repository = self.repository

        class LegacyMiddleware:
            def expire_leases(self, **kwargs):
                return repository.expire_leases(**kwargs)
            def auto_release_wall_budget(self, **kwargs):
                return repository.auto_release_wall_budget({}, **kwargs)
            def auto_requeue_recovering(self, **kwargs):
                return repository.auto_requeue_recovering(**kwargs)
            def lease_next_reconciliation(self, *args, **kwargs):
                return repository.lease_next_reconciliation(*args, **kwargs)

        legacy = LegacyMiddleware()
        dispatcher = SessionLifecycleDispatcher(
            legacy, mock.Mock(), mock.Mock(),
            dispatcher_id="dispatcher:legacy", lease_seconds=30,
        )
        self.assertIsNone(dispatcher.recover_once(now=BASE_TIME))

    def test_termination_requested_for_starting_running_and_collecting_failures(self) -> None:
        starting = self.lease(self.prepare(self.submit()), now=BASE_TIME)
        failed_starting = self.repository.fail_attempt(
            starting["attempt_id"], WORKER, "preflight_failed", now=BASE_TIME
        )
        self.assertEqual(self.repository.get_attempt(starting["attempt_id"])["termination_state"], "requested")
        self.assertEqual(failed_starting["status"], "failed")

        running = self.lease(self.prepare(self.submit(), window_limit=2), now=BASE_TIME)
        self.repository.confirm_attempt_start(running["attempt_id"], WORKER, now=BASE_TIME)
        failed_running = self.repository.fail_attempt(
            running["attempt_id"], WORKER, "runtime_failed", now=BASE_TIME
        )
        self.assertEqual(self.repository.get_attempt(running["attempt_id"])["termination_state"], "requested")
        self.assertEqual(failed_running["status"], "failed")

        collecting = self.lease(self.prepare(self.submit(), window_limit=3), now=BASE_TIME)
        self.repository.confirm_attempt_start(collecting["attempt_id"], WORKER, now=BASE_TIME)
        self.repository.begin_collection(collecting["attempt_id"], WORKER, now=BASE_TIME)
        failed_collecting = self.repository.fail_attempt(
            collecting["attempt_id"], WORKER, "collection_failed", now=BASE_TIME
        )
        self.assertEqual(self.repository.get_attempt(collecting["attempt_id"])["termination_state"], "requested")
        self.assertEqual(failed_collecting["status"], "failed")

    def test_termination_requested_for_reconciling_loss_from_operator_and_wall_budget(self) -> None:
        operator = self._reconciling_attempt(now=BASE_TIME)
        self.repository.force_lost_attempt(operator["attempt_id"], "operator loss", now=BASE_TIME + timedelta(seconds=1))
        self.assertEqual(self.repository.get_attempt(operator["attempt_id"])["termination_state"], "requested")

        wall = self._reconciling_attempt(now=BASE_TIME)
        released = EvaluationMiddleware(self.repository).auto_release_wall_budget(
            now=BASE_TIME + timedelta(seconds=2101)
        )
        self.assertEqual(released[0]["status"], "released")
        self.assertEqual(self.repository.get_attempt(wall["attempt_id"])["status"], "lost")
        self.assertEqual(self.repository.get_attempt(wall["attempt_id"])["termination_state"], "requested")

    def test_termination_state_stays_null_for_unstarted_loss_and_normal_completion(self) -> None:
        leased = self.lease(self.prepare(self.submit()), now=BASE_TIME)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE attempts SET status='leased' WHERE attempt_id=?",
                (leased["attempt_id"],),
            )
            connection.commit()
        self.assertEqual(self.repository.expire_leases(now=BASE_TIME + timedelta(seconds=301)), [leased["attempt_id"]])
        self.assertEqual(self.repository.get_attempt(leased["attempt_id"])["status"], "lost")
        self.assertIsNone(self.repository.get_attempt(leased["attempt_id"])["termination_state"])

        completed = self.lease(self.prepare(self.submit(), window_limit=2), now=BASE_TIME)
        self.repository.confirm_attempt_start(completed["attempt_id"], WORKER, now=BASE_TIME)
        self.repository.begin_collection(completed["attempt_id"], WORKER, now=BASE_TIME)
        self.repository.complete_attempt(
            completed["attempt_id"], WORKER, ["evidence.completed"],
            now=BASE_TIME, _validated_session_result=True,
        )
        self.assertEqual(self.repository.get_attempt(completed["attempt_id"])["status"], "completed")
        self.assertIsNone(self.repository.get_attempt(completed["attempt_id"])["termination_state"])

    def test_lease_expiry_to_reconciling_keeps_termination_state_null(self) -> None:
        for status in ("starting", "running", "collecting"):
            attempt = self.lease(self.prepare(self.submit(), window_limit=4), now=BASE_TIME)
            if status in {"running", "collecting"}:
                self.repository.confirm_attempt_start(attempt["attempt_id"], WORKER, now=BASE_TIME)
            if status == "collecting":
                self.repository.begin_collection(attempt["attempt_id"], WORKER, now=BASE_TIME)
            self.assertEqual(self.repository.expire_leases(now=BASE_TIME + timedelta(seconds=301)), [attempt["attempt_id"]])
            stored = self.repository.get_attempt(attempt["attempt_id"])
            self.assertEqual(stored["status"], "reconciling")
            self.assertIsNone(stored["termination_state"])

    def test_v12_to_v13_migration_adds_termination_state_and_is_idempotent(self) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DELETE FROM schema_migrations WHERE version = 13")
            connection.commit()
        SQLiteEvaluationRepository(self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(attempts)")}
            versions = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        self.assertIn("termination_state", columns)
        self.assertIn(13, versions)

        before_sql, before_rootpage = self._sqlite_index()
        SQLiteEvaluationRepository(self.database)
        after_sql, after_rootpage = self._sqlite_index()
        self.assertEqual(before_sql, after_sql)
        self.assertEqual(before_rootpage, after_rootpage)

    def test_termination_state_and_source_state_enum_invariants(self) -> None:
        self.assertEqual(ATTEMPT_TERMINATION_STATES, frozenset({"requested", "confirmed", "unavailable"}))
        # absent/unknown/indeterminate/unreachable have distinct observation/start-outcome meanings.
        for state in ("absent", "unknown", "indeterminate", "unreachable"):
            self.assertNotIn(state, ATTEMPT_TERMINATION_STATES)
        self.assertEqual(
            TERMINATION_REQUEST_SOURCE_STATES,
            frozenset({"starting", "running", "collecting", "reconciling"}),
        )
        for state in ("leased", "planned", "completed", "failed", "lost", "cancelled"):
            self.assertNotIn(state, TERMINATION_REQUEST_SOURCE_STATES)
        outputs = [attempt_termination_states_sql(ATTEMPT_TERMINATION_STATES) for _ in range(3)]
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1], outputs[2])

if __name__ == "__main__":
    unittest.main()
