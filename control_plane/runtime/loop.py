"""Resilient recover/dispatch/poll runtime loop."""
from __future__ import annotations

import signal
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class RuntimeLoop:
    dispatcher: Any
    min_interval: float = 0.1
    max_interval: float = 30.0
    backoff_factor: float = 2.0
    consecutive_failure_limit: int = 3
    sleep: Callable[[float], None] = time.sleep
    should_continue: Callable[[], bool] | None = None
    errors: list[tuple[str, BaseException]] = field(default_factory=list)
    intervals: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.min_interval < 0 or self.max_interval < self.min_interval or self.backoff_factor < 1:
            raise ValueError("invalid interval/backoff settings")
        if self.consecutive_failure_limit < 1:
            raise ValueError("consecutive_failure_limit must be positive")
        self._stop = False
        self._signal_count = 0
        self._install_signals()

    def _install_signals(self) -> None:
        def handler(signum: int, frame: Any) -> None:
            self._signal_count += 1
            self._stop = True
            if self._signal_count >= 2:
                self._immediate_stop = True
        self._immediate_stop = False
        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError, RuntimeError):
                pass

    def run(self, *, max_rounds: int | None = None) -> int:
        interval = self.min_interval
        failures = 0
        rounds = 0
        while not self._stop and (max_rounds is None or rounds < max_rounds):
            rounds += 1
            progressed = False
            phase_failed = [False, False, False]
            try:
                progressed = progressed or bool(self.dispatcher.recover_once())
            except Exception as exc:
                phase_failed[0] = True
                self.errors.append(("recover_once", exc))
            try:
                progressed = progressed or bool(self.dispatcher.dispatch_once())
            except Exception as exc:
                phase_failed[1] = True
                self.errors.append(("dispatch_once", exc))

            # Re-enumerate every round.  Allocation records contain the
            # authoritative attempt_id (repository contract).
            try:
                middleware = getattr(self.dispatcher, "middleware", None)
                enumerate_active = getattr(middleware, "list_active_allocations", None)
                if not callable(enumerate_active):
                    progressed = progressed or bool(self.dispatcher.poll_once())
                else:
                    allocations = enumerate_active()
                    poll_attempts = 0
                    poll_failures = 0
                    for allocation in allocations:
                        attempt_id = allocation["attempt_id"]
                        poll_attempts += 1
                        try:
                            before = None
                            get_attempt = getattr(middleware, "get_attempt", None)
                            if callable(get_attempt):
                                before_record = get_attempt(attempt_id)
                                before = before_record.get("status") if isinstance(before_record, dict) else None
                            result = self.dispatcher.poll_once(attempt_id)
                            after = result.get("status") if isinstance(result, dict) else None
                            progressed = progressed or (
                                (before is not None and after is not None and before != after)
                                or (before is None and bool(result))
                            )
                        except Exception as exc:
                            poll_failures += 1
                            self.errors.append((f"poll_once:{attempt_id}", exc))
                    if poll_attempts and poll_failures == poll_attempts:
                        phase_failed[2] = True
            except Exception as exc:
                phase_failed[2] = True
                self.errors.append(("poll_once", exc))
            if all(phase_failed):
                failures += 1
            else:
                failures = 0
            if failures >= self.consecutive_failure_limit:
                break
            interval = self.min_interval if progressed else min(
                self.max_interval, interval * self.backoff_factor
            )
            self.intervals.append(interval)
            if self._stop or getattr(self, "_immediate_stop", False):
                break
            if self.should_continue is not None and not self.should_continue():
                break
            self.sleep(interval)
        return rounds


def run_loop(dispatcher: Any, **kwargs: Any) -> int:
    """Convenience wrapper around :class:`RuntimeLoop`."""
    return RuntimeLoop(dispatcher, **kwargs).run()
