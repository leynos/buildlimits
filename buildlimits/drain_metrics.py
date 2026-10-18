"""Observe fixed drain operations without deciding when work is safe to release."""

import contextlib
import typing as typ

from buildlimits.metrics import MAX_COUNTER, VOCABULARIES, Metrics
from buildlimits.sccache import LiveCacheWorkError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.metrics import MetricsPort


class DrainMetrics:
    """Count bounded attempts, faults and operation-level recoveries.

    Parameters
    ----------
    port : MetricsPort | None
        Failure-isolated observer; absent discards observations. It receives no
        request or group identity.
    """

    def __init__(self, port: MetricsPort | None = None) -> None:
        """Reset five fixed operation counters for this supervisor lifetime."""
        self._port = port if port is not None else Metrics()
        self._attempts = dict.fromkeys(VOCABULARIES["drain_operation"], 0)
        self._failures = self._attempts.copy()
        self._recoveries = self._attempts.copy()
        self._faulted = dict.fromkeys(self._attempts, False)

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

    def _record(self, operation: str, state: str, reason: str) -> None:
        """Drop observation failures without retaining exception text."""
        with contextlib.suppress(Exception):
            self._port.emit(
                "drain_progress",
                drain_operation=operation,
                drain_state=state,
                drain_reason=reason,
                drain_attempts=self._attempts[operation],
                drain_failures=self._failures[operation],
                drain_recoveries=self._recoveries[operation],
            )
