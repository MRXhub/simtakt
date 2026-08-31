#!/usr/bin/env python3
"""Integration checks for post-scheduling SessionPlan materialization."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from unittest import mock

from control_plane.core.evaluation_contracts import (
    make_candidate,
    make_evaluation_request,
    make_problem_definition,
)
from control_plane.data.sqlite_evaluation_repository import RepositoryError
from control_plane.evaluation.execution_options import (
    make_execution_option,
    make_execution_option_set,
    make_execution_preparation,
    make_performance_profile,
    make_performance_profile_snapshot,
)
from control_plane.evaluation.control_plane import resolve_control_plane_database
from control_plane.evaluation.governed_preparation import GovernedPreparationError
from control_plane.evaluation.prepared_dispatcher import DispatchError, PreparedExecutionDispatcher
from control_plane.evaluation.execution_topology import ExecutionTopologyError
from control_plane.evaluation.execution_planning import materialize_session_plan
from control_plane.evaluation.scheduling import (
    make_resource_allocation,
    schedule,
    scheduling_decision_plain,
)
from control_plane.evaluation.scheduling_policy import resolve_governed_scheduling_policy
from control_plane.evaluation.service import EvaluationMiddleware
from tests.legacy_prebound_middleware_fixture import (
    LegacyEvaluationMiddleware,
)
from control_plane.simulation.session_contracts import make_simulation_session_plan
from control_plane.simulation.worker import SessionStartFailure
from tests.test_scheduling_policy import write_project


REVISION = "sha256:" + "1" * 64
TARGET = "simulation.remote-primary"
PERFORMANCE_CLASS = "performance-class:sha256:" + "a" * 64


class FakeResourceMonitor:
    def __init__(
        self,
        *,
        processors: int = 4,
        blocked_targets: set[str] | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.processors = processors
        self.blocked_targets = blocked_targets or set()
        self.events = events if events is not None else []

    @contextmanager
    def locked_dispatch(self):
        self.events.append("global")
        yield

    @contextmanager
    def locked_snapshot(self, target_id: str):
        self.events.append(target_id)
        blocked = target_id in self.blocked_targets
        yield {
            "schema_version": 1,
            "snapshot_kind": "resource-snapshot",
            "snapshot_revision": REVISION,
            "target_id": target_id,
            "status": "blocked" if blocked else "ready",
            "available_processors": self.processors,
            "available_memory_bytes": 8 * 1024**3,
            "default_request_memory_bytes": 4 * 1024**3,
            "observed_allocation_keys": [],
            "reasons": [],
            "created_at": "2026-08-03T11:59:00+00:00",
            "lock_held": True,
            "remote_workspace_root": "/remote/test-workspace",
        }

    def record_decision(
        self,
        decision,
        candidates,
        allocations,
        resources,
        *,
        scheduling_policy=None,
        decision_time=None,
        capacity_envelope=None,
    ):
        return "evidence.scheduling.fixture", Path("decision.json")


class RecordingWorker:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.starts: list[tuple[dict, dict, str]] = []

    def start_session(self, plan, allocation, session_ref):
        persisted = EvaluationMiddleware.for_project(self.project_root).get_attempt(
            plan["attempt_id"]
        )
        if (
            persisted["execution_plan"] != plan
            or persisted["allocation"] != allocation
            or persisted["session_ref"] != session_ref
        ):
            raise AssertionError("Worker was called before atomic facts were durable")
        self.starts.append((dict(plan), dict(allocation), session_ref))


class PreparedExecutionDispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        write_project(self.root)
        self.scheduling_policy = resolve_governed_scheduling_policy(self.root)
        self.database = resolve_control_plane_database(self.root)
        self.middleware = EvaluationMiddleware.for_project(self.root)
        problem = make_problem_definition(
            problem_id="prepared-dispatch-fixture",
            parameter_schema_revision=REVISION,
            constraint_revision=REVISION,
            simulation_capabilities=["full-tcad"],
            metric_schema_revision=REVISION,
        )
        self.middleware.register_problem(problem)
        self.candidate = make_candidate(
            problem_id=problem["problem_id"],
            problem_revision=problem["revision"],
            parameters={"x": 1.0},
        )
        request = make_evaluation_request(
            candidate_id=self.candidate["candidate_id"],
            fidelity="full-tcad",
            requested_outputs=["Eff"],
            evidence_profile="fixture-v1",
        )
        self.evaluation = self.middleware.submit(self.candidate, request)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def topology(self, *target_ids: str, same_host: bool = False, missing_host: str | None = None) -> dict:
        records = []
        for index, target_id in enumerate(target_ids):
            host_id = None if target_id == missing_host else ("host-shared" if same_host else f"host-{index}")
            records.append({
                "target_id": target_id,
                "status": "active",
                "formal_execution": True,
                "host_id": host_id,
                "license_pool_id": "pool-fixture",
            })
        return {
            "targets": records,
            "formal_target_ids": list(target_ids),
        }

    def dispatcher(self, monitor: FakeResourceMonitor, topology: dict | None = None) -> PreparedExecutionDispatcher:
        return PreparedExecutionDispatcher(
            self.middleware,
            monitor,
            RecordingWorker(self.root),
            dispatcher_id="dispatcher:global",
            lease_seconds=120,
            preparation_governance=lambda preparation, now: preparation,
            scheduling_policy=self.scheduling_policy,
            execution_topology=topology,
        )

    def execution_preparation(
        self,
        *,
        processors: int = 2,
        evaluation_id: str | None = None,
        candidate_id: str | None = None,
        target_id: str = TARGET,
    ) -> dict:
        option = make_execution_option(
            simulation_definition_artifact_id="simulation-definition.fixture",
            simulation_definition_revision=REVISION,
            runnable_package_artifact_id=f"package.fixture-p{processors}",
            runnable_package_revision=REVISION,
            target_id=target_id,
            processors=processors,
            memory_bytes=4 * 1024**3,
            performance_class_id=PERFORMANCE_CLASS,
        )
        profile = make_performance_profile(
            execution_option_id=option["option_id"],
            evidence_artifact_id="evidence.performance.fixture",
            evidence_revision=REVISION,
            sample_count=2,
            duration_p50_seconds=120,
            duration_p90_seconds=150,
            peak_rss_p90_bytes=2 * 1024**3,
            performance_class_id=PERFORMANCE_CLASS,
        )
        return make_execution_preparation(
            evaluation_id=evaluation_id or self.evaluation["evaluation_id"],
            candidate_id=candidate_id or self.candidate["candidate_id"],
            simulation_proxy="simulation-session-v1",
            numerical_profile="proxy-managed-v1",
            recovery_profile_revision=REVISION,
            task_id="fixture-task",
            authorization_id="authorization.fixture",
            authorization_revision=REVISION,
            command_timeout_seconds=600,
            max_solver_runs=1,
            max_wall_seconds=900,
            execution_option_set=make_execution_option_set([option]),
            performance_profile_snapshot=make_performance_profile_snapshot(
                policy_revision=REVISION,
                profiles=[profile],
            ),
        )

    def prepare(
        self,
        *,
        processors: int = 2,
        target_id: str = TARGET,
        evaluation_id: str | None = None,
        candidate_id: str | None = None,
    ) -> dict:
        return self.middleware._repository.create_prepared_attempt(
            self.execution_preparation(
                processors=processors,
                target_id=target_id,
                evaluation_id=evaluation_id,
                candidate_id=candidate_id,
            )
        )

    def prepare_other_candidate(
        self, *, target_id: str, processors: int = 2, parameter_x: float | None = None
    ) -> dict:
        candidate = make_candidate(
            problem_id=self.candidate["problem_id"],
            problem_revision=self.candidate["problem_revision"],
            parameters={"x": parameter_x if parameter_x is not None else len(self.middleware.prepared_scheduling_candidates()) + 2.0},
        )
        request = make_evaluation_request(
            candidate_id=candidate["candidate_id"],
            fidelity="full-tcad",
            requested_outputs=["Eff"],
            evidence_profile="fixture-v1",
        )
        evaluation = self.middleware.submit(candidate, request)
        return self.prepare(
            processors=processors,
            target_id=target_id,
            evaluation_id=evaluation["evaluation_id"],
            candidate_id=candidate["candidate_id"],
        )

    def claim_materials(
        self, prepared: dict, *, session_ref: str = "session-fixture"
    ) -> tuple[dict, dict, dict, dict]:
        preparation = prepared["execution_preparation"]
        target_id = preparation["execution_option_set"]["options"][0]["target_id"]
        resources = {
            "schema_version": 1,
            "snapshot_kind": "resource-snapshot",
            "snapshot_revision": REVISION,
            "target_id": target_id,
            "status": "ready",
            "available_processors": 4,
            "available_memory_bytes": 8 * 1024**3,
            "default_request_memory_bytes": 4 * 1024**3,
            "observed_allocation_keys": [],
            "reasons": [],
            "created_at": "2026-08-03T11:59:00+00:00",
            "lock_held": True,
            "remote_workspace_root": "/remote/test-workspace",
        }
        decision = schedule(
            [
                {
                    "attempt_id": prepared["attempt_id"],
                    "execution_option_set": preparation["execution_option_set"],
                    "performance_profile_snapshot": preparation[
                        "performance_profile_snapshot"
                    ],
                }
            ],
            [],
            resources,
        )
        option = scheduling_decision_plain(decision["selected_execution_option"])
        plan = materialize_session_plan(
            attempt_id=prepared["attempt_id"],
            preparation=preparation,
            selected_option=option,
        )
        allocation = make_resource_allocation(
            decision,
            session_ref=session_ref,
            run_id="20260731-120000-000",
            remote_workspace_root=resources["remote_workspace_root"],
            decision_artifact_id="evidence.scheduling.fixture",
            decision_artifact_path="decision.json",
        )
        return preparation, option, plan, allocation
    def claim_starting(
        self, prepared: dict, *, owner: str = "dispatcher:fixture",
        session_ref: str = "session-fixture",
    ) -> dict:
        preparation, option, plan, allocation = self.claim_materials(
            prepared, session_ref=session_ref
        )
        claimed = self.middleware.claim_prepared_execution(
            prepared["attempt_id"], owner, 120,
            preparation_id=preparation["preparation_id"],
            selected_option_id=option["option_id"],
            session_plan=plan, allocation=allocation,
            license_sessions=1,
            now=datetime.now(timezone.utc),
        )
        self.assertIsNotNone(claimed)
        return claimed

    def test_claim_stops_at_starting_and_persists_allocation(self) -> None:
        prepared = self.prepare()
        claimed = self.claim_starting(prepared)
        stored = self.middleware.get_attempt(prepared["attempt_id"])
        self.assertEqual(claimed["status"], "starting")
        self.assertEqual(stored["status"], "starting")
        self.assertEqual(stored["allocation"], claimed["allocation"])
        self.assertIsNotNone(stored["allocation"])

    def test_launch_confirmation_reaches_running_and_persists_launch_event(self) -> None:
        prepared = self.prepare()
        self.claim_starting(prepared)
        dispatcher_id = "dispatcher:fixture"
        confirmed = self.middleware.confirm_attempt_start(
            prepared["attempt_id"], dispatcher_id
        )
        self.assertEqual(confirmed["status"], "running")
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                """SELECT payload_json FROM state_events
                   WHERE aggregate_type = 'attempt' AND aggregate_id = ?
                     AND event_type = 'AttemptStarted'
                   ORDER BY sequence DESC LIMIT 1""",
                (prepared["attempt_id"],),
            ).fetchone()
        self.assertIsNotNone(row)
        payload = json.loads(row[0])
        self.assertEqual(payload["reason"], "launch_confirmed")
        self.assertEqual(payload["outcome"], "launch_confirmed")


    def test_unreachable_means_remote_may_have_started_so_reconcile_and_hold_capacity(self) -> None:
        prepared = self.prepare()
        worker = RecordingWorker(self.root)
        worker.start_session = mock.Mock(
            side_effect=SessionStartFailure("unreachable", "transport", "remote unreachable")
        )
        dispatcher = self.dispatcher(FakeResourceMonitor())
        dispatcher.worker = worker
        result = dispatcher.dispatch_once(now=datetime.now(timezone.utc))
        stored = self.middleware.get_attempt(prepared["attempt_id"])
        self.assertEqual(result[0]["status"], "reconciling")
        self.assertEqual(stored["status"], "reconciling")
        self.assertTrue(any(
            item["attempt_id"] == prepared["attempt_id"]
            for item in self.middleware.active_allocations()
        ))
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1
            )

    def test_indeterminate_start_failure_enters_reconciliation(self) -> None:
        prepared = self.prepare()
        worker = RecordingWorker(self.root)
        worker.start_session = mock.Mock(
            side_effect=SessionStartFailure("indeterminate", "transport", "unknown result")
        )
        dispatcher = self.dispatcher(FakeResourceMonitor())
        dispatcher.worker = worker
        dispatcher.dispatch_once(now=datetime.now(timezone.utc))
        self.assertEqual(self.middleware.get_attempt(prepared["attempt_id"])["status"], "reconciling")

    def test_deterministic_start_failures_remain_failed(self) -> None:
        for index, outcome in enumerate(("not_started", "preflight_failed", "absent")):
            with self.subTest(outcome=outcome):
                prepared = (
                    self.prepare()
                    if index == 0
                    else self.prepare_other_candidate(target_id=TARGET, parameter_x=index + 2.0)
                )
                worker = RecordingWorker(self.root)
                worker.start_session = mock.Mock(
                    side_effect=SessionStartFailure(outcome, "deterministic", outcome)
                )
                dispatcher = self.dispatcher(FakeResourceMonitor())
                dispatcher.worker = worker
                dispatcher.dispatch_once(now=datetime.now(timezone.utc))
                self.assertEqual(
                    self.middleware.get_attempt(prepared["attempt_id"])["status"], "failed"
                )

    def test_launch_confirmation_cas_rejects_nonstarting_and_wrong_owner(self) -> None:
        prepared = self.prepare()
        self.claim_starting(prepared)
        self.middleware.fail_attempt(prepared["attempt_id"], "dispatcher:fixture", "fixture")
        with self.assertRaisesRegex(Exception, "requires starting"):
            self.middleware.confirm_attempt_start(prepared["attempt_id"], "dispatcher:fixture")
        self.assertEqual(self.middleware.get_attempt(prepared["attempt_id"])["status"], "failed")

        second = self.prepare_other_candidate(target_id=TARGET)
        self.claim_starting(second, owner="dispatcher:right", session_ref="session-second")
        with self.assertRaisesRegex(Exception, "claiming dispatcher"):
            self.middleware.confirm_attempt_start(second["attempt_id"], "dispatcher:wrong")
        self.assertEqual(self.middleware.get_attempt(second["attempt_id"])["status"], "starting")

    def test_starting_attempt_appears_in_active_allocations_and_holds_capacity(self) -> None:
        prepared = self.prepare()
        self.claim_starting(prepared)
        active = self.middleware.active_allocations(TARGET)
        self.assertEqual([item["attempt_id"] for item in active], [prepared["attempt_id"]])

    def test_plan_option_and_allocation_appear_atomically_after_selection(self) -> None:
        prepared = self.prepare()
        self.assertIsNotNone(prepared["execution_preparation"])
        self.assertIsNone(prepared["selected_execution_option_id"])
        self.assertIsNone(prepared["execution_plan"])
        self.assertIsNone(prepared["allocation"])
        worker = RecordingWorker(self.root)
        dispatcher = PreparedExecutionDispatcher(
            self.middleware,
            FakeResourceMonitor(),
            worker,
            dispatcher_id="dispatcher:global",
            lease_seconds=120,
            preparation_governance=lambda preparation, now: preparation,
            scheduling_policy=self.scheduling_policy,
        )

        started = dispatcher.dispatch_once(
            now=datetime.now(timezone.utc)
        )

        self.assertEqual(len(started), 1)
        started_attempt = started[0]
        self.assertEqual(started_attempt["status"], "running")
        self.assertIsNotNone(started_attempt["selected_execution_option_id"])
        self.assertIsNotNone(started_attempt["execution_plan"])
        self.assertIsNotNone(started_attempt["allocation"])
        self.assertEqual(len(worker.starts), 1)

    def test_worker_start_failure_requires_reconciliation_and_propagates(self) -> None:
        prepared = self.prepare()
        worker = RecordingWorker(self.root)
        worker.start_session = mock.Mock(side_effect=RuntimeError("worker unavailable"))
        dispatcher = PreparedExecutionDispatcher(
            self.middleware,
            FakeResourceMonitor(),
            worker,
            dispatcher_id="dispatcher:global",
            lease_seconds=120,
            preparation_governance=lambda preparation, now: preparation,
            scheduling_policy=self.scheduling_policy,
        )

        with mock.patch.object(
            self.middleware,
            "require_reconciliation",
            wraps=self.middleware.require_reconciliation,
        ) as require_reconciliation:
            with self.assertRaisesRegex(RuntimeError, "worker unavailable"):
                dispatcher.dispatch_once(now=datetime.now(timezone.utc))

        require_reconciliation.assert_called_once()
        self.assertEqual(
            require_reconciliation.call_args.kwargs["reason"],
            "worker-start-indeterminate",
        )
        self.assertEqual(
            self.middleware.get_attempt(prepared["attempt_id"])["status"],
            "reconciling",
        )

    def test_dispatch_revalidates_preparation_before_claim(self) -> None:
        prepared = self.prepare()
        worker = RecordingWorker(self.root)

        def revoked(preparation: dict, now: datetime | None) -> dict:
            raise RuntimeError("preparation authorization revoked")

        dispatcher = PreparedExecutionDispatcher(
            self.middleware,
            FakeResourceMonitor(),
            worker,
            dispatcher_id="dispatcher:global",
            lease_seconds=120,
            preparation_governance=revoked,
            scheduling_policy=self.scheduling_policy,
        )

        with self.assertRaisesRegex(RuntimeError, "authorization revoked"):
            dispatcher.dispatch_once()

        stored = self.middleware.get_attempt(prepared["attempt_id"])
        self.assertEqual(stored["status"], "planned")
        self.assertIsNone(stored["selected_execution_option_id"])
        self.assertIsNone(stored["execution_plan"])
        self.assertIsNone(stored["allocation"])
        self.assertEqual(worker.starts, [])

    def test_rejected_preparation_is_retired_without_blocking_valid_work(self) -> None:
        rejected = self.prepare()
        other_candidate = make_candidate(
            problem_id=self.candidate["problem_id"],
            problem_revision=self.candidate["problem_revision"],
            parameters={"x": 2.0},
        )
        other_request = make_evaluation_request(
            candidate_id=other_candidate["candidate_id"],
            fidelity="full-tcad",
            requested_outputs=["Eff"],
            evidence_profile="fixture-v1",
        )
        other_evaluation = self.middleware.submit(other_candidate, other_request)
        valid = self.middleware._repository.create_prepared_attempt(
            self.execution_preparation(
                evaluation_id=other_evaluation["evaluation_id"],
                candidate_id=other_candidate["candidate_id"],
            )
        )
        rejected_id = rejected["execution_preparation"]["preparation_id"]

        def govern(preparation: dict, now: datetime | None) -> dict:
            if preparation["preparation_id"] == rejected_id:
                raise GovernedPreparationError("fixture authorization revoked")
            return preparation

        worker = RecordingWorker(self.root)
        dispatcher = PreparedExecutionDispatcher(
            self.middleware,
            FakeResourceMonitor(),
            worker,
            dispatcher_id="dispatcher:global",
            lease_seconds=120,
            preparation_governance=govern,
            scheduling_policy=self.scheduling_policy,
        )

        started = dispatcher.dispatch_once(now=datetime.now(timezone.utc))

        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["attempt_id"], valid["attempt_id"])
        self.assertEqual(started[0]["status"], "running")
        self.assertEqual(
            self.middleware.get_attempt(rejected["attempt_id"])["status"],
            "cancelled",
        )
        self.assertEqual(len(worker.starts), 1)

    def test_revocation_after_decision_still_blocks_atomic_claim(self) -> None:
        prepared = self.prepare()
        worker = RecordingWorker(self.root)
        calls = 0

        def revoke_at_claim(preparation: dict, now: datetime | None) -> dict:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError("revoked immediately before claim")
            return preparation

        dispatcher = PreparedExecutionDispatcher(
            self.middleware,
            FakeResourceMonitor(),
            worker,
            dispatcher_id="dispatcher:global",
            lease_seconds=120,
            preparation_governance=revoke_at_claim,
            scheduling_policy=self.scheduling_policy,
        )

        with self.assertRaisesRegex(RuntimeError, "immediately before claim"):
            dispatcher.dispatch_once()

        stored = self.middleware.get_attempt(prepared["attempt_id"])
        self.assertEqual(calls, 3)
        self.assertEqual(stored["status"], "planned")
        self.assertIsNone(stored["selected_execution_option_id"])
        self.assertIsNone(stored["execution_plan"])
        self.assertIsNone(stored["allocation"])
        self.assertEqual(worker.starts, [])

    def test_preparation_replay_returns_one_fully_prepared_attempt(self) -> None:
        preparation = self.execution_preparation()

        first = self.middleware._repository.create_prepared_attempt(preparation)
        second = EvaluationMiddleware.for_project(
            self.root
        )._repository.create_prepared_attempt(preparation)

        self.assertEqual(second["attempt_id"], first["attempt_id"])
        self.assertEqual(second["execution_preparation"], preparation)
        self.assertEqual(
            [item["attempt_id"] for item in self.middleware.prepared_scheduling_candidates()],
            [first["attempt_id"]],
        )
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM attempts WHERE execution_preparation_json IS NULL"
                ).fetchone()[0],
                0,
            )

    def test_concurrent_preparation_is_idempotent_and_never_half_bound(self) -> None:
        preparation = self.execution_preparation()
        clients = [
            EvaluationMiddleware.for_project(self.root),
            EvaluationMiddleware.for_project(self.root),
        ]

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda client: client._repository.create_prepared_attempt(
                        preparation
                    ),
                    clients,
                )
            )

        self.assertEqual(len({result["attempt_id"] for result in results}), 1)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM attempts WHERE execution_preparation_json IS NULL"
                ).fetchone()[0],
                0,
            )

    def test_bind_failure_rolls_back_attempt_and_scheduled_event(self) -> None:
        with mock.patch.object(
            self.middleware._repository,
            "_bind_execution_preparation_in_transaction",
            side_effect=RepositoryError("injected bind failure"),
        ):
            with self.assertRaisesRegex(RepositoryError, "injected bind failure"):
                self.middleware._repository.create_prepared_attempt(
                    self.execution_preparation()
                )

        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM state_events WHERE aggregate_type = 'attempt'"
                ).fetchone()[0],
                0,
            )

    def test_different_preparations_compete_without_half_bound_attempt(self) -> None:
        barrier = Barrier(2)
        clients = [
            EvaluationMiddleware.for_project(self.root),
            EvaluationMiddleware.for_project(self.root),
        ]
        preparations = [
            self.execution_preparation(processors=1),
            self.execution_preparation(processors=2),
        ]

        def compete(item: tuple[EvaluationMiddleware, dict]) -> tuple[str, object]:
            client, preparation = item
            barrier.wait()
            try:
                return "prepared", client._repository.create_prepared_attempt(
                    preparation
                )
            except RepositoryError as exc:
                return "rejected", str(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(compete, zip(clients, preparations)))

        self.assertEqual([kind for kind, _ in outcomes].count("prepared"), 1)
        self.assertEqual([kind for kind, _ in outcomes].count("rejected"), 1)
        rejection = next(value for kind, value in outcomes if kind == "rejected")
        self.assertIn("different prepared Attempt", rejection)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM attempts WHERE execution_preparation_json IS NULL"
                ).fetchone()[0],
                0,
            )

    def test_invalid_preparation_rolls_back_attempt_creation(self) -> None:
        other_candidate = make_candidate(
            problem_id=self.candidate["problem_id"],
            problem_revision=self.candidate["problem_revision"],
            parameters={"x": 99.0},
        )

        with self.assertRaisesRegex(RepositoryError, "different Candidate"):
            self.middleware._repository.create_prepared_attempt(
                self.execution_preparation(
                    candidate_id=other_candidate["candidate_id"]
                )
            )

        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0
            )

    def test_legacy_control_cannot_lease_or_prebind_prepared_attempt(self) -> None:
        prepared = self.prepare()
        preparation, option, plan, allocation = self.claim_materials(prepared)
        legacy = LegacyEvaluationMiddleware.from_sqlite(self.database)

        self.assertTrue(hasattr(legacy, "complete_attempt"))
        self.assertIsNone(legacy.lease_next("legacy-worker", 120))
        with self.assertRaisesRegex(RepositoryError, "legacy scheduling cannot adopt"):
            legacy.schedule_attempt(
                self.evaluation["evaluation_id"],
                simulation_adapter="simulation-session-v1",
                numerical_profile="proxy-managed-v1",
            )
        with self.assertRaisesRegex(
            RepositoryError, "legacy SessionPlan cannot be bound"
        ):
            legacy.bind_session_plan(plan)

        stored = self.middleware.get_attempt(prepared["attempt_id"])
        self.assertEqual(stored["execution_preparation"], preparation)
        self.assertIsNone(stored["selected_execution_option_id"])
        self.assertIsNone(stored["execution_plan"])
        self.assertIsNone(stored["allocation"])

        claimed = self.middleware.claim_prepared_execution(
            prepared["attempt_id"],
            "dispatcher:prepared",
            120,
            preparation_id=preparation["preparation_id"],
            selected_option_id=option["option_id"],
            session_plan=plan,
            allocation=allocation,
        )
        self.assertIsNotNone(claimed)
        self.middleware.confirm_attempt_start(
            prepared["attempt_id"], "dispatcher:prepared"
        )
        self.middleware.begin_collection(
            prepared["attempt_id"], "dispatcher:prepared"
        )
        with self.assertRaisesRegex(RepositoryError, "raw completion cannot complete"):
            legacy.complete_attempt(
                prepared["attempt_id"],
                "dispatcher:prepared",
                ["artifact.fake"],
            )
        with self.assertRaisesRegex(RepositoryError, "validated SessionResult"):
            legacy.repository.complete_attempt(
                prepared["attempt_id"],
                "dispatcher:prepared",
                ["artifact.fake"],
            )
        self.assertEqual(
            self.middleware.get_attempt(prepared["attempt_id"])["status"],
            "collecting",
        )

    def test_prepared_control_cannot_adopt_legacy_attempt(self) -> None:
        legacy = LegacyEvaluationMiddleware.from_sqlite(self.database)
        legacy_attempt = legacy.schedule_attempt(
            self.evaluation["evaluation_id"],
            simulation_adapter="simulation-session-v1",
            numerical_profile="proxy-managed-v1",
        )

        with self.assertRaisesRegex(RepositoryError, "cannot adopt"):
            self.middleware._repository.create_prepared_attempt(
                self.execution_preparation()
            )

        stored = legacy.get_attempt(legacy_attempt["attempt_id"])
        self.assertIsNone(stored["execution_preparation"])
        self.assertIsNone(stored["execution_plan"])

    def test_recovery_can_reuse_preparation_for_a_new_attempt(self) -> None:
        preparation_contract = self.execution_preparation()
        first = self.middleware._repository.create_prepared_attempt(
            preparation_contract
        )
        preparation, option, plan, allocation = self.claim_materials(first)
        claimed = self.middleware.claim_prepared_execution(
            first["attempt_id"],
            "dispatcher:retry",
            120,
            preparation_id=preparation["preparation_id"],
            selected_option_id=option["option_id"],
            session_plan=plan,
            allocation=allocation,
        )
        self.assertIsNotNone(claimed)
        self.middleware.fail_attempt(
            first["attempt_id"], "dispatcher:retry", "solver-failed"
        )
        self.middleware.plan_recovery(
            self.evaluation["evaluation_id"], "retry with the same approved options"
        )

        second = self.middleware._repository.create_prepared_attempt(
            preparation_contract,
            checkpoint_parent_attempt_id=first["attempt_id"],
        )

        self.assertNotEqual(second["attempt_id"], first["attempt_id"])
        self.assertEqual(second["attempt_number"], 2)
        self.assertEqual(
            second["execution_preparation_id"],
            first["execution_preparation_id"],
        )
        self.assertEqual(
            second["checkpoint_parent_attempt_id"], first["attempt_id"]
        )

    def test_wait_does_not_materialize_plan_or_allocate(self) -> None:
        prepared = self.prepare(processors=2)
        worker = RecordingWorker(self.root)
        dispatcher = PreparedExecutionDispatcher(
            self.middleware,
            FakeResourceMonitor(processors=1),
            worker,
            dispatcher_id="dispatcher:global",
            lease_seconds=120,
            preparation_governance=lambda preparation, now: preparation,
            scheduling_policy=self.scheduling_policy,
        )

        result = dispatcher.dispatch_once()
        stored = self.middleware.get_attempt(prepared["attempt_id"])

        self.assertEqual(result, [])
        self.assertIsNone(stored["selected_execution_option_id"])
        self.assertIsNone(stored["execution_plan"])
        self.assertIsNone(stored["allocation"])
        self.assertEqual(worker.starts, [])

    def test_mismatched_materialized_plan_rolls_back_every_execution_fact(self) -> None:
        prepared = self.prepare()
        preparation, option, plan, allocation = self.claim_materials(prepared)
        wrong_plan = make_simulation_session_plan(
            attempt_id=plan["attempt_id"],
            evaluation_id=plan["evaluation_id"],
            candidate_id=plan["candidate_id"],
            simulation_proxy=plan["simulation_proxy"],
            recovery_profile_revision=plan["recovery_profile_revision"],
            base_package_artifact_id=plan["base_package"]["artifact_id"],
            base_package_revision=plan["base_package"]["revision"],
            task_id=plan["task_id"],
            target_id=plan["target_id"],
            authorization_id=plan["authorization"]["artifact_id"],
            authorization_revision=plan["authorization"]["revision"],
            requested_processors=plan["resources"]["requested_processors"] + 1,
            command_timeout_seconds=plan["budget"]["command_timeout_seconds"],
            max_solver_runs=plan["budget"]["max_solver_runs"],
            max_wall_seconds=plan["budget"]["max_wall_seconds"],
        )

        with self.assertRaisesRegex(
            RepositoryError, "not materialized from the selected option"
        ):
            self.middleware.claim_prepared_execution(
                prepared["attempt_id"],
                "dispatcher:malicious",
                120,
                preparation_id=preparation["preparation_id"],
                selected_option_id=option["option_id"],
                session_plan=wrong_plan,
                allocation=allocation,
            )

        stored = self.middleware.get_attempt(prepared["attempt_id"])
        self.assertEqual(stored["status"], "planned")
        self.assertIsNone(stored["selected_execution_option_id"])
        self.assertIsNone(stored["execution_plan"])
        self.assertIsNone(stored["allocation"])
        self.assertIsNone(stored["session_ref"])

    def test_concurrent_dispatchers_can_claim_prepared_attempt_only_once(self) -> None:
        prepared = self.prepare()
        preparation, option, plan, allocation = self.claim_materials(prepared)

        def claim(dispatcher_id: str):
            middleware = EvaluationMiddleware.for_project(self.root)
            return middleware.claim_prepared_execution(
                prepared["attempt_id"],
                dispatcher_id,
                120,
                preparation_id=preparation["preparation_id"],
                selected_option_id=option["option_id"],
                session_plan=plan,
                allocation=allocation,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ["dispatcher:one", "dispatcher:two"]))

        self.assertEqual(sum(result is not None for result in results), 1)
        stored = self.middleware.get_attempt(prepared["attempt_id"])
        self.assertEqual(stored["status"], "starting")
        self.assertEqual(stored["selected_execution_option_id"], option["option_id"])
        self.assertEqual(stored["execution_plan"], plan)
        self.assertEqual(stored["allocation"], allocation)

    def test_two_formal_targets_launch_once_each_in_one_round(self) -> None:
        first = self.prepare(target_id="target-a")
        second = self.prepare_other_candidate(target_id="target-b")
        topology = self.topology("target-a", "target-b")
        result = self.dispatcher(FakeResourceMonitor(), topology).dispatch_once(
            now=datetime.now(timezone.utc)
        )
        self.assertEqual({item["attempt_id"] for item in result}, {first["attempt_id"], second["attempt_id"]})
        self.assertEqual({item["status"] for item in result}, {"running"})

    def test_blocked_target_does_not_prevent_other_target_launch(self) -> None:
        blocked = self.prepare(target_id="target-a")
        ready = self.prepare_other_candidate(target_id="target-b")
        topology = self.topology("target-a", "target-b")
        result = self.dispatcher(
            FakeResourceMonitor(blocked_targets={"target-a"}), topology
        ).dispatch_once(now=datetime.now(timezone.utc))
        self.assertEqual([item["attempt_id"] for item in result], [ready["attempt_id"]])
        self.assertEqual(self.middleware.get_attempt(blocked["attempt_id"])["status"], "planned")

    def test_license_limit_blocks_all_targets_when_one_allocation_is_active(self) -> None:
        active = self.prepare(target_id="target-a")
        preparation, option, plan, allocation = self.claim_materials(active)
        self.assertIsNotNone(
            self.middleware.claim_prepared_execution(
                active["attempt_id"], "dispatcher:fixture", 120,
                preparation_id=preparation["preparation_id"],
                selected_option_id=option["option_id"],
                session_plan=plan, allocation=allocation,
            )
        )
        waiting = self.prepare_other_candidate(target_id="target-b")
        policy = self.scheduling_policy.as_mapping()
        policy["capacity_envelope"]["license_sessions"] = 1
        topology = self.topology("target-a", "target-b")
        with mock.patch.object(type(self.scheduling_policy), "as_mapping", return_value=policy):
            result = self.dispatcher(FakeResourceMonitor(), topology).dispatch_once()
        self.assertEqual(result, [])
        self.assertEqual(self.middleware.get_attempt(waiting["attempt_id"])["status"], "planned")

    def test_dispatch_claim_uses_platform_license_share(self) -> None:
        prepared = self.prepare()
        policy = self.scheduling_policy.as_mapping()
        policy["capacity_envelope"]["license_reserve"] = 1
        claim = self.middleware.claim_prepared_execution
        with mock.patch.object(
            type(self.scheduling_policy), "as_mapping", return_value=policy
        ), mock.patch.object(
            self.middleware,
            "claim_prepared_execution",
            wraps=claim,
        ) as claimed:
            result = self.dispatcher(FakeResourceMonitor()).dispatch_once()

        self.assertEqual([item["attempt_id"] for item in result], [prepared["attempt_id"]])
        self.assertEqual(claimed.call_args.kwargs["license_sessions"], 5)

    def test_same_host_capacity_scope_accounts_for_other_target_allocation(self) -> None:
        active = self.prepare(target_id="target-b")
        preparation, option, plan, allocation = self.claim_materials(active)
        self.assertIsNotNone(
            self.middleware.claim_prepared_execution(
                active["attempt_id"], "dispatcher:fixture", 120,
                preparation_id=preparation["preparation_id"],
                selected_option_id=option["option_id"],
                session_plan=plan, allocation=allocation,
            )
        )
        waiting = self.prepare_other_candidate(target_id="target-a")
        topology = self.topology("target-a", "target-b", same_host=True)
        result = self.dispatcher(FakeResourceMonitor(processors=2), topology).dispatch_once()
        self.assertEqual(result, [])
        self.assertEqual(self.middleware.get_attempt(waiting["attempt_id"])["status"], "planned")

    def test_multi_target_missing_host_fails_before_any_launch(self) -> None:
        first = self.prepare(target_id="target-a")
        second = self.prepare_other_candidate(target_id="target-b")
        topology = self.topology("target-a", "target-b", missing_host="target-b")
        worker = RecordingWorker(self.root)
        monitor = FakeResourceMonitor()
        dispatcher = PreparedExecutionDispatcher(
            self.middleware, monitor, worker, dispatcher_id="dispatcher:global",
            lease_seconds=120, preparation_governance=lambda preparation, now: preparation,
            scheduling_policy=self.scheduling_policy, execution_topology=topology,
        )
        with self.assertRaises(ExecutionTopologyError):
            dispatcher.dispatch_once()
        self.assertEqual(worker.starts, [])
        self.assertEqual(self.middleware.get_attempt(first["attempt_id"])["status"], "planned")
        self.assertEqual(self.middleware.get_attempt(second["attempt_id"])["status"], "planned")

    def test_multi_target_monitor_without_global_lock_fails_closed(self) -> None:
        self.prepare(target_id="target-a")
        self.prepare_other_candidate(target_id="target-b")
        monitor = FakeResourceMonitor()
        monitor.locked_dispatch = None
        with self.assertRaises(DispatchError):
            self.dispatcher(monitor, self.topology("target-a", "target-b")).dispatch_once()

    def test_global_lock_is_acquired_before_each_target_lock(self) -> None:
        self.prepare(target_id="target-a")
        self.prepare_other_candidate(target_id="target-b")
        events: list[str] = []
        result = self.dispatcher(
            FakeResourceMonitor(events=events), self.topology("target-a", "target-b")
        ).dispatch_once()
        self.assertEqual(len(result), 2)
        self.assertEqual(events[:3], ["global", "target-a", "target-b"])

    def test_new_dispatcher_ignores_legacy_prebound_session_plan(self) -> None:
        legacy_middleware = LegacyEvaluationMiddleware.from_sqlite(self.database)
        legacy = legacy_middleware.plan_prepared_session(
            self.evaluation["evaluation_id"],
            simulation_adapter="simulation-session-v1",
            numerical_profile="proxy-managed-v1",
            recovery_profile_revision=REVISION,
            base_package_artifact_id="package.legacy.fixture",
            base_package_revision=REVISION,
            task_id="legacy-fixture",
            target_id=TARGET,
            authorization_id="authorization.fixture",
            authorization_revision=REVISION,
            requested_processors=2,
            command_timeout_seconds=600,
            max_solver_runs=1,
            max_wall_seconds=900,
        )
        dispatcher = PreparedExecutionDispatcher(
            self.middleware,
            FakeResourceMonitor(),
            RecordingWorker(self.root),
            dispatcher_id="dispatcher:global",
            lease_seconds=120,
            preparation_governance=lambda preparation, now: preparation,
            scheduling_policy=self.scheduling_policy,
        )

        self.assertEqual(dispatcher.dispatch_once(), [])
        stored = self.middleware.get_attempt(legacy["attempt_id"])
        self.assertEqual(stored["status"], "planned")
        self.assertIsNotNone(stored["execution_plan"])
        self.assertIsNone(stored["allocation"])


if __name__ == "__main__":
    unittest.main()
