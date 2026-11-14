"""Decide when a repeated drain success still deserves its own record."""

import time
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# A healthy supervisor terminates and empty-checks every finished group, so a
# record per success would dominate the journal. Repeated successes are
# summarized at most this often, which keeps the lifetime counters current.
ROUTINE_SUMMARY_SECONDS = 600.0
COMPLETED = "completed"


class SuccessSummary:
    """Suppress success records that only repeat a recent success.

    Parameters
    ----------
    clock : Callable[[], float]
        Monotonic seconds.
    interval : float
        Minimum seconds between records of repeated successes.

    Notes
    -----
    Only a ``completed`` state that follows a recorded ``completed`` state is a
    repeat. A failure, pending live work or a recovery changes the operation's
    last recorded state, so it is always recorded and restarts the interval.
    """

    def __init__(
        self,
        clock: cabc.Callable[[], float] = time.monotonic,
        interval: float = ROUTINE_SUMMARY_SECONDS,
    ) -> None:
        """Start with no operation having recorded a state."""
        self._clock = clock
        self._interval = interval
        self._last_state: dict[str, str] = {}
        self._last_completed: dict[str, float] = {}

    def is_repeat(self, operation: str, state: str) -> bool:
        """Report whether recording ``state`` would add nothing but counters.

        Parameters
        ----------
        operation : str
            The fixed drain operation the record describes.
        state : str
            The state the record would carry.

        Returns
        -------
        bool
            True when the operation's last record was ``completed`` less than
            ``interval`` seconds ago.

        Examples
        --------
        >>> summary = SuccessSummary(clock=lambda: 5.0)
        >>> summary.recorded("terminate", "completed")
        >>> summary.is_repeat("terminate", "completed")
        True
        """
        if state != COMPLETED or self._last_state.get(operation) != COMPLETED:
            return False
        last = self._last_completed.get(operation)
        return last is not None and self._clock() - last < self._interval

    def recorded(self, operation: str, state: str) -> None:
        """Remember the state a record was written for.

        Parameters
        ----------
        operation : str
            The fixed drain operation the record describes.
        state : str
            The state the written record carries.
        """
        self._last_state[operation] = state
        if state == COMPLETED:
            self._last_completed[operation] = self._clock()
