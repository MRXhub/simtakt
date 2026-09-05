import unittest
from datetime import datetime, timezone
from unittest import mock

from control_plane.evaluation.dispatcher import SessionLifecycleDispatcher


class OrphanRecoveryDispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.middleware = mock.Mock()
        self.resource_monitor = mock.Mock()
        self.worker = mock.Mock()
        self.dispatcher = SessionLifecycleDispatcher(
            self.middleware,
            self.resource_monitor,
            self.worker,
            dispatcher_id="dispatcher:test",
            lease_seconds=30,
        )
        self.now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
        self.naive = datetime(2026, 9, 4, 12, 0)

    def test_direct_call_reconcile_open_orphans_naive_now_raises_value_error(self) -> None:
        # Finding 5: direct call to _reconcile_open_orphans with naive now
        with self.assertRaises(ValueError) as ctx:
            self.dispatcher._reconcile_open_orphans(now=self.naive)
        self.assertIn("timezone-aware now", str(ctx.exception))

    def test_direct_call_reconcile_one_orphan_naive_now_raises_value_error(self) -> None:
        # Finding 5: direct call to _reconcile_one_orphan with naive now
        orphan = {
            "orphan_id": "orphan-1",
            "status": "open",
            "created_at": "2026-09-04T11:00:00+00:00",
            "metadata": {},
        }
        with self.assertRaises(ValueError) as ctx:
            self.dispatcher._reconcile_one_orphan(orphan, self.naive)
        self.assertIn("timezone-aware now", str(ctx.exception))

    def test_collect_orphan_failure_logs_error_records_event_and_returns_false(self) -> None:
        # Finding 2: collect raises -> orphan stays open, failure surfaced, next orphan still processed
        orphan_1 = {
            "orphan_id": "orphan-1",
            "attempt_id": "attempt-1",
            "session_ref": "sess-1",
            "status": "open",
            "created_at": "2026-09-04T10:00:00+00:00",
            "metadata": {"last_observed_status": "completed"},
        }
        orphan_2 = {
            "orphan_id": "orphan-2",
            "attempt_id": "attempt-2",
            "session_ref": "sess-2",
            "status": "open",
            "created_at": "2026-09-04T10:01:00+00:00",
            "metadata": {"last_observed_status": "completed"},
        }
        self.middleware.list_orphan_sessions.return_value = [orphan_1, orphan_2]
        self.middleware.get_orphan_session.side_effect = lambda oid: (
            orphan_1 if oid == "orphan-1" else orphan_2
        )
        self.middleware.get_attempt.return_value = {
            "execution_plan": {"tasks": []},
            "allocation": {"target": "mock"},
        }
        def observe_session(session_ref):
            return "completed"

        self.worker.observe_session.side_effect = observe_session

        def fake_collect(session_ref):
            if session_ref == "sess-1":
                raise RuntimeError("simulated collect failure")
            return ({"output": 42}, "artifact-2")

        self.worker.collect_session.side_effect = fake_collect
        self.middleware.harvest_orphan_session = mock.Mock()

        # Direct call to _collect_orphan on orphan_1 must return False
        meta_1 = dict(orphan_1["metadata"])
        with self.assertLogs("control_plane.evaluation.dispatcher", level="ERROR") as cm:
            res = self.dispatcher._collect_orphan(orphan_1, meta_1, self.now)
        self.assertFalse(res)
        self.assertTrue(any("simulated collect failure" in log for log in cm.output))
        # Best-effort event recorded
        self.middleware.record_orphan_state_event.assert_called_with(
            "orphan-1",
            from_status="open",
            to_status="open",
            event_type="OrphanCollectFailed",
            payload=mock.ANY,
            now=self.now,
        )
        payload = self.middleware.record_orphan_state_event.call_args[1]["payload"]
        self.assertEqual(payload["error"]["type"], "RuntimeError")

        # In full reconcile run, orphan-1 failure leaves it open and surfaces failure,
        # but next orphan (orphan-2) is still processed.
        self.middleware.record_orphan_state_event.reset_mock()
        processed = self.dispatcher._reconcile_open_orphans(now=self.now)
        self.assertEqual(processed, 1)
        self.middleware.harvest_orphan_session.assert_called_once_with(
            {"output": 42}, "dispatcher:test", "artifact-2", "sess-2", now=self.now
        )

    def test_terminate_orphan_unavailable_updates_status_before_event_and_logs_event_failure(self) -> None:
        # Unavailable termination must retain capacity, updating status FIRST,
        # record OrphanTerminationUnavailable event AFTER; event failure logged, not silent.
        orphan = {
            "orphan_id": "orphan-1",
            "attempt_id": "attempt-1",
            "session_ref": "sess-1",
            "status": "open",
            "created_at": "2026-09-04T10:00:00+00:00",
            "metadata": {
                "last_observed_status": "running",
                "kill_at": "2026-09-04T11:00:00+00:00",
            },
        }
        del self.worker.terminate_session

        call_order = []
        def fake_update(oid, **kwargs):
            call_order.append(("update", oid, kwargs.get("status")))
        def fake_record(oid, **kwargs):
            call_order.append(("record", oid, kwargs.get("event_type")))
            raise RuntimeError("event persistence failed")

        self.middleware.update_orphan_session.side_effect = fake_update
        self.middleware.record_orphan_state_event.side_effect = fake_record

        meta = dict(orphan["metadata"])
        with self.assertLogs("control_plane.evaluation.dispatcher", level="ERROR") as cm:
            res = self.dispatcher._terminate_orphan(orphan, meta, self.now)

        self.assertTrue(res)
        # update must be FIRST, record AFTER
        self.assertEqual(call_order, [
            ("update", "orphan-1", "open"),
            ("record", "orphan-1", "OrphanTerminationUnavailable"),
        ])
        self.assertNotIn("closed_at", meta)
        self.assertEqual(self.middleware.record_orphan_state_event.call_args.kwargs["to_status"], "open")
        # Event failure must be logged at error, not silent
        self.assertTrue(any("event persistence failed" in log for log in cm.output))

    def test_expired_running_observation_is_persisted_when_termination_unavailable(self) -> None:
        orphan = {
            "orphan_id": "orphan-live", "status": "open", "session_ref": "session-live",
            "metadata": {"kill_at": "2026-09-04T11:00:00+00:00"},
        }
        self.middleware.get_orphan_session.return_value = orphan
        del self.worker.terminate_session
        with mock.patch.object(self.dispatcher, "_observe_orphan", return_value="running"):
            self.dispatcher._reconcile_one_orphan(orphan, self.now)
        update = self.middleware.update_orphan_session.call_args.kwargs
        self.assertEqual(update["status"], "open")
        self.assertEqual(update["metadata"]["last_observed_status"], "running")
        self.assertEqual(update["metadata"]["last_observed_at"], self.now.isoformat())

    def test_outer_per_orphan_exception_handler_logs_exception(self) -> None:
        # Finding 6: Outer per-orphan exception handler adds logger.exception alongside event
        orphan = {
            "orphan_id": "orphan-crash",
            "attempt_id": "attempt-crash",
            "status": "open",
            "created_at": "2026-09-04T10:00:00+00:00",
            "metadata": {},
        }
        self.middleware.list_orphan_sessions.return_value = [orphan]
        self.dispatcher._reconcile_one_orphan = mock.Mock(
            side_effect=RuntimeError("unexpected crash in reconcile_one")
        )
        with self.assertLogs("control_plane.evaluation.dispatcher", level="ERROR") as cm:
            self.dispatcher._reconcile_open_orphans(now=self.now)

        self.assertTrue(any("unexpected crash in reconcile_one" in log for log in cm.output))
        self.middleware.record_orphan_state_event.assert_called_once_with(
            "orphan-crash",
            from_status="open",
            to_status="open",
            event_type="OrphanObserveFailed",
            payload=mock.ANY,
            now=self.now,
        )


if __name__ == "__main__":
    unittest.main()
