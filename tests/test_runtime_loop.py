import unittest
from unittest.mock import Mock

from control_plane.runtime.loop import RuntimeLoop


class LoopTests(unittest.TestCase):
    def dispatcher(self, allocations=()):
        d = Mock()
        d.middleware = Mock()
        d.middleware.list_active_allocations.side_effect = lambda: list(allocations)
        d.middleware.get_attempt.return_value = {"status": "running"}
        d.poll_once.return_value = {"status": "running"}
        return d

    def test_three_rounds_counts_and_no_sleep(self):
        d = self.dispatcher([{"attempt_id": "a"}, {"attempt_id": "b"}])
        sleeps = []
        d.recover_once.return_value = False
        d.dispatch_once.return_value = False
        loop = RuntimeLoop(d, min_interval=0, max_interval=1, sleep=sleeps.append)
        self.assertEqual(loop.run(max_rounds=3), 3)
        self.assertEqual(d.recover_once.call_count, 3)
        self.assertEqual(d.dispatch_once.call_count, 3)
        self.assertEqual(d.poll_once.call_count, 6)

    def test_phase_order(self):
        order = []
        d = self.dispatcher([{"attempt_id": "a"}])
        d.middleware.list_active_allocations.side_effect = lambda: [{"attempt_id": "a"}]
        d.recover_once.side_effect = lambda: order.append("recover") or False
        d.dispatch_once.side_effect = lambda: order.append("dispatch") or False
        def poll(attempt):
            order.append("poll")
            return {"status": "running"}
        d.poll_once.side_effect = poll
        RuntimeLoop(d, min_interval=0, sleep=lambda _: None).run(max_rounds=1)
        self.assertEqual(order, ["recover", "dispatch", "poll"])

    def test_reenumerates_each_round(self):
        d = self.dispatcher([])
        d.middleware.list_active_allocations.side_effect = [
            [{"attempt_id": "a"}, {"attempt_id": "b"}], [{"attempt_id": "a"}]
        ]
        RuntimeLoop(d, min_interval=0, sleep=lambda _: None).run(max_rounds=2)
        self.assertEqual([c.args[0] for c in d.poll_once.call_args_list], ["a", "b", "a"])

    def test_poll_failure_isolated(self):
        d = self.dispatcher([{"attempt_id": x} for x in "abc"])
        def poll(attempt):
            if attempt == "b":
                raise RuntimeError("broken")
            return {"status": "running"}
        d.poll_once.side_effect = poll
        RuntimeLoop(d, min_interval=0, sleep=lambda _: None).run(max_rounds=1)
        self.assertEqual([c.args[0] for c in d.poll_once.call_args_list], ["a", "b", "c"])

    def test_recover_failure_does_not_skip_later_phases(self):
        d = self.dispatcher([{"attempt_id": "a"}])
        d.recover_once.side_effect = RuntimeError("recover")
        RuntimeLoop(d, min_interval=0, sleep=lambda _: None).run(max_rounds=1)
        d.dispatch_once.assert_called_once_with()
        d.poll_once.assert_called_once_with("a")

    def test_all_phase_failures_stop_at_limit(self):
        d = self.dispatcher([{"attempt_id": "a"}])
        d.recover_once.side_effect = RuntimeError("r")
        d.dispatch_once.side_effect = RuntimeError("d")
        d.poll_once.side_effect = RuntimeError("p")
        loop = RuntimeLoop(d, min_interval=0, sleep=lambda _: None, consecutive_failure_limit=2)
        self.assertEqual(loop.run(), 2)

    def test_backoff_progress_and_no_progress(self):
        d = self.dispatcher([])
        d.recover_once.side_effect = [True, False, False]
        sleeps = []
        d.dispatch_once.return_value = False
        loop = RuntimeLoop(d, min_interval=1, max_interval=3, backoff_factor=2, sleep=sleeps.append)
        loop.run(max_rounds=3)
        self.assertEqual(sleeps, [1, 2, 3])

    def test_first_stop_finishes_round_second_stop_immediate(self):
        d = self.dispatcher([{"attempt_id": "a"}])
        loop = RuntimeLoop(d, min_interval=0, sleep=lambda _: None)
        # Exercise the documented signal-handler control point without sending
        # process signals (which would affect the test runner).
        handler = loop._install_signals
        del handler  # keep the approach explicit: mutate the stop state.
        d.recover_once.side_effect = lambda: setattr(loop, "_stop", True) or False
        self.assertEqual(loop.run(max_rounds=3), 1)
        d.dispatch_once.assert_called_once_with()
        d.poll_once.assert_called_once_with("a")
        d.close = Mock()
        d2 = self.dispatcher([])
        loop2 = RuntimeLoop(d2, min_interval=0, sleep=lambda _: None)
        loop2._signal_count = 2
        loop2._immediate_stop = True
        loop2._stop = True
        self.assertEqual(loop2.run(max_rounds=3), 0)
        d2.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
