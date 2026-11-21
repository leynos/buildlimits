"""A ledger that matches each fixture row to the writer's own answer.

``StoreWriter.append`` never blocks: a full queue refuses the row and counts it
as discarded, while the worker publishes a discard for a row it could not
write. Aggregate results cannot say which row a discard belongs to, so a
fixture must not read one as a refusal of the row it just offered. This ledger
spends none of the queue's slack: it keeps fewer rows in flight than the queue
can hold, which makes a refusal impossible, and fails the test on any discard
rather than re-offering a row that may already be stored.
"""

import time
import typing as typ

import pytest

from buildlimits.outcome_store import MAX_APPEND_QUEUE, StoreWriter

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import StoreRecord

__all__ = ["AppendLedger"]

_QUEUE_HEADROOM = MAX_APPEND_QUEUE - 1
_APPEND_WAIT_SECONDS = 2.0
_APPEND_POLL_SECONDS = 0.005


class AppendLedger:
    """Count a fixture's append calls against the writer's published results."""

    def __init__(self, writer: StoreWriter) -> None:
        """Track one writer's answers to the rows offered through it."""
        self._writer = writer
        self._calls = 0
        self._stored = 0
        self._discarded = 0

    def append(self, row: StoreRecord) -> None:
        """Offer one row once the queue can accept it."""
        self._await_outstanding(at_most=_QUEUE_HEADROOM - 1)
        self._calls += 1
        self._writer.append(row)

    def settle(self, expected: int) -> None:
        """Wait until every offered row is answered without a discard.

        Parameters
        ----------
        expected : int
            The number of rows the fixture offered.
        """
        self._await_outstanding(at_most=0)
        if self._stored != expected:
            pytest.fail(f"the writer stored {self._stored} of {expected} fixture rows")

    def _await_outstanding(self, *, at_most: int) -> None:
        """Wait, within the append bound, until few enough rows are unanswered.

        A discard fails the test instead of being retried: the aggregate
        result does not identify its row, and re-offering the row just
        appended would store that row twice while the discarded one stays
        missing.
        """
        deadline = time.monotonic() + _APPEND_WAIT_SECONDS
        waiting = self._drain()
        while waiting > at_most:
            if time.monotonic() >= deadline:
                pytest.fail(f"the writer left {waiting} fixture rows unanswered")
            time.sleep(_APPEND_POLL_SECONDS)
            waiting = self._drain()

    def _drain(self) -> int:
        """Add the writer's published append outcomes to the counters.

        Returns
        -------
        int
            Rows offered through this ledger and not yet answered.
        """
        for result in self._writer.results():
            if result.operation == "append":
                self._stored += result.records
                self._discarded += result.discarded
        if self._discarded:
            pytest.fail(
                f"the writer discarded {self._discarded} fixture rows; "
                "the history would be shorter than the fixture asked for"
            )
        return self._calls - self._stored
