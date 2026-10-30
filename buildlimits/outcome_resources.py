"""Own the recorder's helper reaper and asynchronous store writer.

The recorder runs on the supervisor's selector thread. D17 allows a bounded
join only while the supervisor shuts down, so the two paths are kept apart by
name: ``service_failed`` never waits, and only ``close`` spends a deadline.
Each step is protected on its own, because one failing cleanup must not skip
the other or escape into admission decisions.
"""

import contextlib
import time
import typing as typ

from buildlimits.supervisor_outcome_setup import OUTCOME_CLOSE_SECONDS

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.nonblocking_child import ChildReaper
    from buildlimits.outcome_key import Salt
    from buildlimits.outcome_ports import OutcomeStorePort, StoreFailure, StoreResult
    from buildlimits.outcome_record_format import StoreRecord

_REAP_INTERVAL_SECONDS = 0.01


class OutcomeResources:
    """Reap the statistics helper and close the store within a bound."""

    def __init__(
        self,
        reaper: ChildReaper,
        writer: OutcomeStorePort,
        clock: cabc.Callable[[], float],
    ) -> None:
        """Bind the two owned resources and the supervisor's monotonic clock."""
        self._reaper = reaper
        self._writer = writer
        self._clock = clock

    @property
    def reaper(self) -> ChildReaper:
        """The reaper shared with the statistics query port.

        Returns
        -------
        ChildReaper
            The one reaper that owns the query helper processes.
        """
        return self._reaper

    def poll(self, now: float) -> None:
        """Poll every owned helper once at the caller's tick time."""
        self._reaper.poll(now)

    def service_failed(self) -> None:
        """Advance cancelled cleanup without waiting for it to finish.

        Examples
        --------
        A stalled writer accepts ``close(0.0)`` and returns immediately.
        """
        with contextlib.suppress(Exception):
            self._reaper.poll(self._clock())
        with contextlib.suppress(Exception):
            self._writer.close(0.0)

    def close(self, timeout: float) -> None:
        """Reap the helper, then give the remaining deadline to the store.

        Examples
        --------
        ``close(1.0)`` returns within one second whatever the writer does.
        """
        wait = min(max(timeout, 0.0), OUTCOME_CLOSE_SECONDS)
        deadline = time.monotonic() + wait
        with contextlib.suppress(Exception):
            self._reap_until(deadline)
        with contextlib.suppress(Exception):
            self._writer.close(max(0.0, deadline - time.monotonic()))

    def _reap_until(self, deadline: float) -> None:
        """Poll the reaper until it is empty or the shutdown deadline passes."""
        while True:
            self._reaper.poll(self._clock())
            if not self._reaper.active_count or time.monotonic() >= deadline:
                return
            time.sleep(
                min(_REAP_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic()))
            )

    def salt_result(self) -> Salt | StoreFailure | None:
        """Return the asynchronous, one-shot salt handoff without waiting.

        Returns
        -------
        Salt | StoreFailure | None
            Startup key material, its closed failure, or no result yet.
        """
        return self._writer.salt_result()

    def results(self) -> tuple[StoreResult, ...]:
        """Drain aggregated writer results without waiting for I/O.

        Returns
        -------
        tuple[StoreResult, ...]
            Pending counts grouped by operation and closed reason.
        """
        return self._writer.results()

    def append(self, record: StoreRecord) -> None:
        """Enqueue one record without waiting for disk activity."""
        self._writer.append(record)


__all__ = ["OutcomeResources"]
