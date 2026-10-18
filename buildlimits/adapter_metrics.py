"""One silent, identifier-free summary for a non-nested adapter invocation."""

import contextlib
import typing as typ

from buildlimits.journal_native import JournalSink
from buildlimits.metrics import MAX_COUNTER, Metrics

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.metrics import MetricsPort


class AdapterMetrics:
    """Measure one invocation and isolate emission from compiler behaviour.

    Parameters
    ----------
    clock : Callable[[], float]
        The heartbeat's monotonic clock.
    started : float
        The heartbeat's invocation start timestamp.
    metrics : MetricsPort | None
        Injected port; absent means a silent native journal emitter.

    Attributes
    ----------
    outcome : str
        The terminal outcome, initially ``supervisor_lost``.
    """

    def __init__(
        self,
        clock: cabc.Callable[[], float],
        started: float,
        metrics: MetricsPort | None = None,
    ) -> None:
        """Bind the clock and choose a silent adapter transport."""
        self.clock = clock
        self.started = started
        self._sink = (
            JournalSink(identifier="build-limits-adapter") if metrics is None else None
        )
        self.metrics = (
            metrics
            if metrics is not None
            else Metrics(typ.cast("JournalSink", self._sink), source="adapter")
        )
        self.outcome = "supervisor_lost"
        self.attempts = 0
        self.busy_retries = 0
        self.restart_retries = 0
        self.admitted: float | None = None
        self._finished = False

    def attempted(self) -> None:
        """Count one connection attempt without retaining its destination."""
        self.attempts = min(MAX_COUNTER, self.attempts + 1)

    def retry_busy(self) -> None:
        """Count a fresh admission attempt after a bounded busy refusal."""
        self.busy_retries = min(MAX_COUNTER, self.busy_retries + 1)

    def retry_restart(self) -> None:
        """Count a fresh admission attempt after the supervisor went away."""
        self.restart_retries = min(MAX_COUNTER, self.restart_retries + 1)

    def admission(self, timestamp: float) -> None:
        """Record the first admission boundary.

        Parameters
        ----------
        timestamp : float
            The heartbeat's transition timestamp.
        """
        if self.admitted is None:
            self.admitted = timestamp

    def interrupted(self) -> None:
        """Emit before signal reproduction can terminate this process."""
        self.outcome = "interrupted"
        self.finish()

    def finish(self) -> None:
        """Emit at most once, shielding exit status and signals from failures."""
        if self._finished:
            return
        self._finished = True
        # An injected transport or clock must be as harmless as the default sink.
        try:
            with contextlib.suppress(Exception):
                now = self.clock()
                admitted = self.admitted
                self.metrics.emit(
                    "adapter_finished",
                    outcome=self.outcome,
                    connection_attempts=self.attempts,
                    busy_retries=self.busy_retries,
                    restart_retries=self.restart_retries,
                    wait_seconds=(now if admitted is None else admitted) - self.started,
                    runtime_seconds=0.0 if admitted is None else now - admitted,
                )
        finally:
            if self._sink is not None:
                with contextlib.suppress(Exception):
                    self._sink.close()
