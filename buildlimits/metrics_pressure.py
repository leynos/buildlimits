"""Forward events unchanged and publish supervisor gauges on a cadence.

Two adapters sit at the metrics boundary. :class:`EventForwarder` is a port
adapter: it sends each event to the underlying validating emitter and does
nothing else, so forwarding cannot sample, publish or hold state.
:class:`GaugePublisher` owns the :class:`~buildlimits.metrics_gauges.GaugeAccumulator`
and the cached snapshot reader, and writes the one periodic ``gauges`` record.

The split keeps the write visible at its call sites. Only the supervisor's tick,
admission boundary and shutdown call the publisher, and each call names whether
it samples, publishes when due, or forces the final record.

This application adapter maps domain snapshots to the journal schema. It has
no authority over admission, descriptor ownership or listener recovery.
"""

import contextlib
import enum
import time
import typing as typ

from buildlimits.metrics import gauge_fields
from buildlimits.metrics_gauges import GaugeAccumulator

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.metrics import MetricsPort
    from buildlimits.supervisor import Supervisor


class Publication(enum.StrEnum):
    """What one publication attempt achieved.

    Attributes
    ----------
    WRITTEN : str
        The ``gauges`` record reached its destination.
    NOT_DUE : str
        The cadence does not call for a record at this instant.
    ENDPOINT_UNREAD : str
        No sample was read at the interval's endpoint, so none may close it.
    REFUSED : str
        The destination refused the record; the interval stays open.
    SUPPRESSED : str
        The attempt raised and the boundary contained it; the interval stays
        open, exactly as for a refusal.
    """

    WRITTEN = "written"
    NOT_DUE = "not-due"
    ENDPOINT_UNREAD = "endpoint-unread"
    REFUSED = "refused"
    SUPPRESSED = "suppressed"

    @property
    def is_written(self) -> bool:
        """Report whether the record reached its destination.

        Returns
        -------
        bool
            Whether this outcome is :attr:`WRITTEN`.
        """
        return self is Publication.WRITTEN


class EventForwarder:
    """Forward each event to the validating emitter unchanged.

    Parameters
    ----------
    port : MetricsPort
        Existing validating or recording emitter.

    Notes
    -----
    The forwarder holds no snapshot and no interval state, so an event is
    never a publication point. A failing transport is reported as ``False``
    rather than raised, because telemetry cannot affect admission or compiler
    results.
    """

    def __init__(self, port: MetricsPort) -> None:
        """Bind the underlying emitter."""
        self._port = port

    def emit(self, event: str, **fields: object) -> bool:
        """Forward one event unchanged.

        Parameters
        ----------
        event : str
            Existing fixed catalogue event.
        **fields : object
            Event-specific bounded measurements, forwarded verbatim.

        Returns
        -------
        bool
            Whether the underlying port took the record.
        """
        with contextlib.suppress(Exception):
            return bool(self._port.emit(event, **fields))
        return False


class GaugePublisher:
    """Sample the cached supervisor state and publish periodic snapshots.

    Parameters
    ----------
    port : MetricsPort
        Existing validating or recording emitter.
    snapshot : Callable[[], Mapping[str, object]]
        Reads cached producer state only; failures leave the interval open.
    clock : Callable[[], float]
        Monotonic time sampled by callers that do not pass their own reading.
    accumulator : GaugeAccumulator | None
        Interval peaks, queue integral and cadence; absent starts a new
        supervisor lifetime.

    Notes
    -----
    Supervisor-wide counters leave this port only on ``gauges`` records. The
    interval stays open across a failed sample or emission, so a later record
    covers both intervals; retry deferral lives in the accumulator because it
    is part of the same cadence policy, and moving it to the caller would let
    a failing sink be retried on every tick.
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

    def sample(self, now: float) -> bool:
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

    def publish(self, now: float, *, force: bool = False) -> Publication:
        """Publish the folded interval when one is due.

        Parameters
        ----------
        now : float
            Monotonic seconds at which the attempt runs; the caller samples at
            this instant first so the interval ends where its integral does.
        force : bool
            Whether shutdown requires the final record regardless of cadence.

        Returns
        -------
        Publication
            What the attempt achieved. A failed observation or emission leaves
            the interval open for the next attempt.
        """
        latest = self._latest
        # Settling an interval whose endpoint was never read would publish the
        # gap as zero depth and drop the area that follows it, so the interval
        # stays open until a sample reaches the endpoint.
        if latest is None or latest[1] < now:
            return Publication.ENDPOINT_UNREAD
        if not self._accumulator.is_due(now, force=force):
            return Publication.NOT_DUE
        snapshot, sampled_at = latest
        record = self._accumulator.record(snapshot, sampled_at)
        accepted = False
        with contextlib.suppress(Exception):
            accepted = bool(self._port.emit("gauges", **record))
        self._accumulator.settle(snapshot, sampled_at, accepted=accepted)
        return Publication.WRITTEN if accepted else Publication.REFUSED

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
            self._accumulator.observe(None, now)
            return None
        self._accumulator.observe(snapshot, now)
        # A service poll inside a tick may already have sampled at a later
        # clock reading than the tick's own, so the cached endpoint only moves
        # forward; the interval that follows starts where the last one ended.
        latest = self._latest
        if latest is None or now >= latest[1]:
            self._latest = (snapshot, now)
        return snapshot


def supervisor_publisher(supervisor: Supervisor, port: MetricsPort) -> GaugePublisher:
    """Bind delayed cached-state reads after supervisor construction completes.

    Parameters
    ----------
    supervisor : Supervisor
        Owner of admission, request-budget and listener state.
    port : MetricsPort
        Caller-supplied emission boundary.

    Returns
    -------
    GaugePublisher
        Publisher that neither samples resources nor changes domain state.
    """
    return GaugePublisher(port, lambda: _snapshot(supervisor), clock=supervisor.clock)


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
