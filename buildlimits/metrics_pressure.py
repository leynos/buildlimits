"""Publish cached supervisor pressure observations as periodic snapshots.

This application adapter maps domain snapshots to the journal schema. It has
no authority over admission, descriptor ownership or listener recovery.
"""

import contextlib
import time
import typing as typ

from buildlimits.metrics import gauge_fields
from buildlimits.metrics_gauges import GaugeAccumulator

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.metrics import MetricsPort
    from buildlimits.supervisor import Supervisor


class PressureMetrics:
    """Forward events unchanged and publish supervisor gauges periodically.

    Parameters
    ----------
    port : MetricsPort
        Existing validating or recording emitter.
    snapshot : Callable[[], Mapping[str, object]]
        Reads cached producer state only; failures leave the interval open.
    clock : Callable[[], float]
        Monotonic time sampled when an event is forwarded. The supervisor tick
        and shutdown paths pass their own reading to :meth:`observe` and
        :meth:`flush` instead.
    accumulator : GaugeAccumulator | None
        Interval peaks, queue integral and cadence; absent starts a new
        supervisor lifetime.

    Notes
    -----
    Supervisor-wide counters leave this port only on ``gauges`` records. Every
    other event carries exactly the fields its producer supplied.
    """

    def __init__(
        self,
        port: MetricsPort,
        snapshot: cabc.Callable[[], cabc.Mapping[str, object]],
        *,
        clock: cabc.Callable[[], float] = time.monotonic,
        accumulator: GaugeAccumulator | None = None,
    ) -> None:
        """Bind the emitter, snapshot reader and interval state."""
        self._port = port
        self._snapshot = snapshot
        self._clock = clock
        self._accumulator: GaugeAccumulator = (
            accumulator if accumulator is not None else GaugeAccumulator()
        )
        # One field holds the sample and its time so "did the endpoint get
        # read" is a single question rather than two that can disagree.
        self._latest: tuple[dict[str, object], float] | None = None

    def emit(self, event: str, **fields: object) -> bool:
        """Forward one event unchanged after folding the current sample.

        Parameters
        ----------
        event : str
            Existing fixed catalogue event.
        **fields : object
            Event-specific bounded measurements, forwarded verbatim.

        Returns
        -------
        bool
            Whether the underlying port took the record. A failed snapshot
            leaves the event unchanged; only a failed transport reports
            ``False``.
        """
        self._sample(self._clock())
        with contextlib.suppress(Exception):
            return bool(self._port.emit(event, **fields))
        return False

    def observe(self, now: float) -> bool:
        """Fold the current supervisor sample into the open interval.

        Parameters
        ----------
        now : float
            Monotonic seconds at which the supervisor read the snapshot.

        Returns
        -------
        bool
            Whether a sample was available to fold.
        """
        return self._sample(now) is not None

    def flush(self, now: float, *, force: bool = False) -> bool:
        """Publish the folded interval when one is due.

        Parameters
        ----------
        now : float
            Monotonic seconds at which the attempt runs; the caller observes at
            this instant first so the interval ends where its integral does.
        force : bool
            Whether shutdown requires the final record regardless of cadence.

        Returns
        -------
        bool
            Whether a ``gauges`` record reached its destination. A failed
            observation or emission leaves the interval open for the next
            attempt.
        """
        latest = self._latest
        # Settling an interval whose endpoint was never read would publish the
        # gap as zero depth and drop the area that follows it, so the interval
        # stays open until a sample reaches the endpoint.
        if latest is None or latest[1] < now:
            return False
        if not self._accumulator.is_due(now, force=force):
            return False
        snapshot, sampled_at = latest
        record = self._accumulator.record(snapshot, sampled_at)
        accepted = False
        with contextlib.suppress(Exception):
            accepted = bool(self._port.emit("gauges", **record))
        self._accumulator.settle(snapshot, sampled_at, accepted=accepted)
        return accepted

    def _sample(self, now: float) -> dict[str, object] | None:
        """Read and fold one snapshot without letting a reader fault escape.

        Parameters
        ----------
        now : float
            Monotonic seconds at which the snapshot was read.

        Returns
        -------
        dict[str, object] | None
            The folded sample, or ``None`` when the reader raised.
        """
        try:
            snapshot = dict(self._snapshot())
        except Exception:  # ruff: ignore[blind-except] - observation cannot affect admission.
            return None
        self._accumulator.observe(snapshot, now)
        self._latest = (snapshot, now)
        return snapshot


def supervisor_metrics(supervisor: Supervisor, port: MetricsPort) -> PressureMetrics:
    """Bind delayed cached-state reads after supervisor construction completes.

    Parameters
    ----------
    supervisor : Supervisor
        Owner of admission, request-budget and listener state.
    port : MetricsPort
        Caller-supplied emission boundary.

    Returns
    -------
    PressureMetrics
        Observer that neither samples resources nor changes domain state.
    """
    return PressureMetrics(port, lambda: _snapshot(supervisor), clock=supervisor.clock)


def _snapshot(supervisor: Supervisor) -> dict[str, object]:
    """Read only cached domain and application observations.

    Returns
    -------
    dict[str, object]
        Fixed gauge names mapped to cached producer measurements.
    """
    limits = supervisor.requests.descriptors
    return {
        **gauge_fields(supervisor.admission.counts()),
        "fd_retained": supervisor.requests.retained,
        "fd_budget": limits.budget,
        "fd_high_water": limits.high_water,
        "fd_budget_known": limits.baseline_known,
        "accept_paused": supervisor.pressure.paused,
    }
