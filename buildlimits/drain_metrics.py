"""Observe fixed drain operations without deciding when work is safe to release."""

import contextlib
import time
import typing as typ

from buildlimits.cache_backend import LiveCacheWorkError
from buildlimits.metrics import MAX_COUNTER, VOCABULARIES, Metrics

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.metrics import MetricsPort

# A healthy supervisor terminates and empty-checks every finished group, so a
# record per success would dominate the journal. Repeated successes are
# summarized at most this often, which keeps the lifetime counters current.
ROUTINE_SUMMARY_SECONDS = 600.0


class DrainMetrics:
    """Count bounded attempts, faults and operation-level recoveries.

    Parameters
    ----------
    port : MetricsPort | None
        Failure-isolated observer; absent discards observations. It receives no
        request or group identity.
    clock : Callable[[], float]
        Monotonic seconds, used only to space routine success records.
    routine_summary_seconds : float
        Minimum interval between records of repeated successes.

    Notes
    -----
    Failures, pending live work and recoveries are always recorded. A success
    is recorded when it changes the operation's last recorded state, or when
    ``routine_summary_seconds`` have passed since that operation's last
    success record.
    """

    def __init__(
        self,
        port: MetricsPort | None = None,
        *,
        clock: cabc.Callable[[], float] = time.monotonic,
        routine_summary_seconds: float = ROUTINE_SUMMARY_SECONDS,
    ) -> None:
        """Reset five fixed operation counters for this supervisor lifetime."""
        self._port = port if port is not None else Metrics()
        self._clock = clock
        self._routine_summary_seconds = routine_summary_seconds
        self._attempts = dict.fromkeys(VOCABULARIES["drain_operation"], 0)
        self._failures = self._attempts.copy()
        self._recoveries = self._attempts.copy()
        self._faulted = dict.fromkeys(self._attempts, False)
        self._last_state: dict[str, str | None] = dict.fromkeys(self._attempts)
        self._last_completed: dict[str, float | None] = dict.fromkeys(self._attempts)

    def call[T](
        self,
        operation: str,
        action: cabc.Callable[[], T],
        *,
        pending: cabc.Callable[[T], bool] | None = None,
    ) -> T:
        """Execute once, report a fixed outcome, and preserve errors unchanged.

        Parameters
        ----------
        operation : str
            One of the five fixed drain operations; unknown operations execute
            without allocating observer state or emitting a label.
        action : Callable[[], T]
            Existing control or probe operation.
        pending : Callable[[T], bool] | None
            Classifies successful live-work results; never changes the result.

        Returns
        -------
        T
            The original action result.

        Notes
        -----
        Action exceptions propagate unchanged.
        """
        if operation not in self._attempts:
            return action()
        self._increment(self._attempts, operation)
        try:
            result = action()
        except Exception as error:
            self._failed(operation, error)
            raise
        self._succeeded(operation, result, pending)
        return result

    def control_cycle(self, action: cabc.Callable[[], None]) -> None:
        """Observe faults and full-cycle recovery without healthy-loop spam.

        Parameters
        ----------
        action : Callable[[], None]
            A complete selector, dispatch and tick cycle.

        Notes
        -----
        Action exceptions propagate unchanged.
        """
        if self._faulted["control_loop"]:
            self.call("control_loop", action)
            return
        try:
            action()
        except Exception as error:
            self._increment(self._attempts, "control_loop")
            self._failed("control_loop", error)
            raise

    def _failed(self, operation: str, error: Exception) -> None:
        """Classify live work separately from an actual control or probe fault."""
        if isinstance(error, LiveCacheWorkError):
            self._record(operation, "pending", "live_work")
            return
        self._increment(self._failures, operation)
        self._faulted[operation] = True
        reason = "os_error" if isinstance(error, OSError) else "control_error"
        self._record(operation, "failed", reason)

    def _succeeded[T](
        self,
        operation: str,
        result: T,
        pending: cabc.Callable[[T], bool] | None,
    ) -> None:
        """Observe successful work without letting classifier failures escape."""
        with contextlib.suppress(Exception):
            if pending is not None and pending(result):
                self._record(operation, "pending", "live_work")
                return
            state = "completed"
            if self._faulted[operation]:
                self._increment(self._recoveries, operation)
                self._faulted[operation] = False
                state = "recovered"
            self._record(operation, state, "none")

    @staticmethod
    def _increment(counters: dict[str, int], operation: str) -> None:
        """Saturate a fixed operation counter without wrapping."""
        counters[operation] = min(MAX_COUNTER, counters[operation] + 1)

    def _is_routine_repeat(self, operation: str, state: str) -> bool:
        """Report whether a success only repeats a recent success record.

        Returns
        -------
        bool
            True when the record would add nothing but a newer counter value.
        """
        if state != "completed" or self._last_state[operation] != "completed":
            return False
        last = self._last_completed[operation]
        return last is not None and (
            self._clock() - last < self._routine_summary_seconds
        )

    def _record(self, operation: str, state: str, reason: str) -> None:
        """Drop observation failures without retaining exception text."""
        with contextlib.suppress(Exception):
            if self._is_routine_repeat(operation, state):
                return
            self._last_state[operation] = state
            if state == "completed":
                self._last_completed[operation] = self._clock()
            self._port.emit(
                "drain_progress",
                drain_operation=operation,
                drain_state=state,
                drain_reason=reason,
                drain_attempts=self._attempts[operation],
                drain_failures=self._failures[operation],
                drain_recoveries=self._recoveries[operation],
            )
