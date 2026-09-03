from __future__ import annotations

import importlib.util
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from control_plane.core.evaluation_contracts import ContractError
from control_plane.evaluation.dispatcher import SessionLifecycleDispatcher
from control_plane.evaluation.service import EvaluationMiddleware
from control_plane.simulation.worker import (
    normalize_session_observation,
    normalize_session_termination,
)
from tests import test_rolling_window_repository as _rolling
from tests.test_adapter_local_process_example import process_alive

BASE_TIME = _rolling.BASE_TIME


class TerminationConsumptionTests(unittest.TestCase):
    """Focused contract tests for the dispatcher termination queue."""

    def setUp(self) -> None:
        self.fx = _rolling.RollingWindowRepositoryTests()
        self.fx.setUp()

    def tearDown(self) -> None:
        self.fx.tearDown()
    def _requested(self):
        attempt = self.fx._reconciling_attempt(now=BASE_TIME)
        self.fx.repository.force_lost_attempt(attempt["attempt_id"], "test loss", now=BASE_TIME)
        return self.fx.repository.get_attempt(attempt["attempt_id"])

    def _dispatcher(self, worker):
        return SessionLifecycleDispatcher(
            EvaluationMiddleware(self.fx.repository), mock.Mock(), worker,
            dispatcher_id="dispatcher:termination-test", lease_seconds=30,
        )

    def _txn_counter(self):
        calls = []
        original = self.fx.repository._transaction
        def counted(*args, **kwargs):
            calls.append(True)
            return original(*args, **kwargs)
        return calls, counted

    def test_recover_once_idle_opens_zero_write_transactions(self):
        calls, counted = self._txn_counter()
        with mock.patch.object(self.fx.repository, "_transaction", counted):
            self.assertIsNone(self._dispatcher(mock.Mock()).recover_once(now=BASE_TIME))
    def test_unreachable_requested_opens_zero_write_transactions(self):

        attempt = self._requested()
        worker = mock.Mock()
        worker.terminate_session.return_value = "unreachable"
        calls, counted = self._txn_counter()
        with mock.patch.object(self.fx.repository, "_transaction", counted):
            self._dispatcher(worker).recover_once(now=BASE_TIME)
        self.assertEqual(len(calls), 0)
        self.assertEqual(self.fx.repository.get_attempt(attempt["attempt_id"])["termination_state"], "requested")

    def test_termination_outcome_mapping(self):
        for outcome, expected in (("terminated", "confirmed"), ("absent", "confirmed"),
                                  ("unreachable", "requested"), ("indeterminate", "requested")):
            attempt = self._requested()
            worker = mock.Mock()
            worker.terminate_session.return_value = outcome
            self._dispatcher(worker).recover_once(now=BASE_TIME)
            self.assertEqual(self.fx.repository.get_attempt(attempt["attempt_id"])["termination_state"], expected)

    def test_unreachable_is_retried(self):
        attempt = self._requested()
        worker = mock.Mock()
        worker.terminate_session.return_value = "unreachable"
        dispatcher = self._dispatcher(worker)
        dispatcher.recover_once(now=BASE_TIME)
        dispatcher.recover_once(now=BASE_TIME + timedelta(seconds=1))
        self.assertEqual(worker.terminate_session.call_count, 2)
        self.assertEqual(self.fx.repository.get_attempt(attempt["attempt_id"])["termination_state"], "requested")

    def test_worker_without_termination_capability_becomes_unavailable(self):
        attempt = self._requested()
        worker = object()
        dispatcher = self._dispatcher(worker)
        dispatcher.recover_once(now=BASE_TIME)
        dispatcher.recover_once(now=BASE_TIME + timedelta(seconds=1))
        self.assertEqual(self.fx.repository.get_attempt(attempt["attempt_id"])["termination_state"], "unavailable")

    def test_termination_exception_is_isolated_and_retried(self):
        attempt = self._requested()
        worker = mock.Mock()
        worker.terminate_session.side_effect = RuntimeError("adapter down")
        dispatcher = self._dispatcher(worker)
        self.assertIsNotNone(dispatcher.recover_once(now=BASE_TIME))
        self.assertEqual(self.fx.repository.get_attempt(attempt["attempt_id"])["termination_state"], "requested")

    def test_termination_exception_does_not_skip_lease_expiry(self):
        expired = self.fx.lease(self.fx.prepare(self.fx.submit()), now=BASE_TIME)
        requested = self._requested()
        worker = mock.Mock()
        worker.observe_session.return_value = "indeterminate"
        worker.terminate_session.side_effect = RuntimeError("adapter down")
        self._dispatcher(worker).recover_once(now=BASE_TIME + timedelta(seconds=301))
        self.assertEqual(self.fx.repository.get_attempt(expired["attempt_id"])["status"], "reconciling")
        self.assertEqual(self.fx.repository.get_attempt(requested["attempt_id"])["termination_state"], "requested")

    def test_normalize_termination_rejects_invalid_values(self):
        for value in ("killed", "", None, 123):
            with self.assertRaises(ContractError):
                normalize_session_termination(value)

    def test_normalize_termination_matches_observation_whitespace_case(self):
        for value in (" TERMINATED ", " AbSeNt ", " UNREACHABLE ", " Indeterminate "):
            self.assertEqual(normalize_session_termination(value), str(value).strip().lower())
        for value in (" RUNNING ", " Completed ", " ABSENT "):
            self.assertEqual(normalize_session_observation(value), str(value).strip().lower())

    def test_invalid_worker_termination_is_not_allowed_to_crash_recovery(self):
        attempt = self._requested()
        worker = mock.Mock()
        worker.terminate_session.return_value = "killed"
        dispatcher = self._dispatcher(worker)
        try:
            dispatcher.recover_once(now=BASE_TIME)
        except ContractError as exc:
            self.fail(f"invalid adapter result crashed recover_once: {exc}")
        self.assertEqual(self.fx.repository.get_attempt(attempt["attempt_id"])["termination_state"], "requested")

    def test_local_process_adapter_terminates_real_process(self):
        spec = importlib.util.spec_from_file_location(
            "local_process_adapter", Path(__file__).parents[1] / "examples/adapter-local-process/adapter.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        worker = module.SimulationWorker()
        attempt = self._requested()
        with tempfile.TemporaryDirectory() as root:
            allocation = dict(attempt["allocation"])
            allocation["workspace_root"] = root
            worker.start_session(attempt["execution_plan"], allocation, attempt["session_ref"])
            pid = worker._procs[attempt["session_ref"]].pid
            time.sleep(0.15)
            self._dispatcher(worker).recover_once(now=BASE_TIME)
            self.assertIs(process_alive(pid), False)
            worker._procs[attempt["session_ref"]].wait(timeout=5)
            self.assertIs(process_alive(pid), False)


if __name__ == "__main__":
    unittest.main()
