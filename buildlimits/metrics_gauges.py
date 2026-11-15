"""Fold supervisor gauge samples into periodic, bounded journal snapshots.

Per-request records must not repeat counters that describe the whole
supervisor. The supervisor still samples that state on every tick and on every
event, so this module keeps one accumulator per supervisor process: it folds
each sample into interval peaks and a producer-side integral of queue depth,
and decides when the next snapshot is due.

The accumulator is pure. Callers supply monotonic time from the supervisor's
injected clock, and the snapshot itself leaves through the same
failure-isolated metrics port as every other event.

Notes
-----
A failed emission keeps its peaks and integral so the next record covers both
intervals. That is why ``observe`` and ``settle`` are separate: only delivery
decides whether the interval is complete.
"""

import dataclasses as dc
import typing as typ

from buildlimits.metrics_schema import MAX_COUNTER

if typ.TYPE_CHECKING:
    import collections.abc as cabc

GAUGE_INTERVAL_SECONDS = 30.0
GAUGE_STEP = 4
GAUGE_MIN_SPACING_SECONDS = 5.0

# Counts whose movement by a whole step earns a record between heartbeats.
_STEP_FIELDS = (
    "queued",
    "held",
    "running",
    "draining",
    "server_drain",
    "fd_retained",
)


def _count(value: object) -> int | None:
    """Read a bounded, non-negative count from a snapshot field.

    Parameters
    ----------
    value : object
        One value from a supervisor snapshot.

    Returns
    -------
    int | None
        The count, or no value when the sample is missing or invalid.

    Examples
    --------
    >>> _count(2), _count(True), _count(-1)
    (2, None, None)
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return min(value, MAX_COUNTER) if value >= 0 else None


def _wider(peak: int | None, sample: int) -> int:
    """Keep the larger of an interval peak and a new sample.

    Parameters
    ----------
    peak : int | None
        Peak folded so far, or ``None`` before the first sample.
    sample : int
        Newly observed count.

    Returns
    -------
    int
        The larger count.

    Examples
    --------
    >>> _wider(None, 3), _wider(3, 1)
    (3, 3)
    """
    return sample if peak is None else max(peak, sample)


@dc.dataclass(slots=True)
class GaugeAccumulator:
    """Interval peaks, the queue integral and the snapshot cadence.

    Parameters
    ----------
    interval : float
        Heartbeat cadence in monotonic seconds.
    step : int
        Change in a count that makes an earlier record worth writing.
    min_spacing : float
        Minimum spacing between step-triggered records, and the retry delay
        after a failed emission.

    Attributes
    ----------
    queued_seconds : float
        Integral of queue depth over the interval since the last record.
    queued_peak : int | None
        Largest queue depth observed in the interval.
    held_peak : int | None
        Largest held-token count observed in the interval.
    queued_high_water : int
        Largest queue depth observed for this supervisor's lifetime.
    """

    interval: float = GAUGE_INTERVAL_SECONDS
    step: int = GAUGE_STEP
    min_spacing: float = GAUGE_MIN_SPACING_SECONDS
    queued_seconds: float = 0.0
    queued_peak: int | None = None
    held_peak: int | None = None
    queued_high_water: int = 0
    _previous_queued: int | None = None
    _previous_time: float | None = None
    _flushed: float | None = None
    _flushed_counts: dict[str, int] = dc.field(default_factory=dict)
    _stepped: bool = False
    _deferred_until: float = 0.0

    def observe(self, snapshot: cabc.Mapping[str, object], now: float) -> None:
        """Fold one supervisor sample into the open interval.

        Parameters
        ----------
        snapshot : Mapping[str, object]
            Current supervisor-wide values; missing fields stay missing.
        now : float
            Monotonic seconds at which the snapshot was read.
        """
        queued = _count(snapshot.get("queued"))
        self._integrate(now)
        if queued is None:
            # An unreadable depth cannot be integrated as if it were zero, so
            # the gap stays out of the integral until the next sample.
            self._previous_queued = None
            self._previous_time = None
        else:
            self._previous_queued = queued
            self._previous_time = now
            self.queued_high_water = max(self.queued_high_water, queued)
            self.queued_peak = _wider(self.queued_peak, queued)
        held = _count(snapshot.get("held"))
        if held is not None:
            self.held_peak = _wider(self.held_peak, held)
        if self._crossed_a_step(snapshot):
            self._stepped = True

    def is_due(self, now: float, *, force: bool = False) -> bool:
        """Report whether a snapshot should be attempted at *now*.

        Parameters
        ----------
        now : float
            Monotonic seconds at which the attempt would run.
        force : bool
            Whether shutdown requires the final record regardless of cadence.

        Returns
        -------
        bool
            Whether a record is due.
        """
        if force:
            return True
        if self._flushed is None:
            # The first record is a baseline: it claims no interval it did not
            # observe, and it anchors every later interval at one instant.
            return True
        if now < self._deferred_until:
            return False
        elapsed = max(0.0, now - self._flushed)
        if elapsed >= self.interval:
            return True
        return self._stepped and elapsed >= self.min_spacing

    def record(
        self, snapshot: cabc.Mapping[str, object], now: float
    ) -> dict[str, object]:
        """Build the next snapshot record from the folded interval.

        Parameters
        ----------
        snapshot : Mapping[str, object]
            Current supervisor-wide values, copied into the record.
        now : float
            Monotonic seconds at which the record is built.

        Returns
        -------
        dict[str, object]
            The snapshot fields plus peaks, the lifetime queue maximum, the
            covered interval and the queue integral.
        """
        fields: dict[str, object] = dict(snapshot)
        fields["queued_seconds"] = self.queued_seconds
        fields["interval_seconds"] = (
            0.0 if self._flushed is None else max(0.0, now - self._flushed)
        )
        fields["queued_high_water"] = self.queued_high_water
        if self.queued_peak is not None:
            fields["queued_peak"] = self.queued_peak
        if self.held_peak is not None:
            fields["held_peak"] = self.held_peak
        return fields

    def settle(
        self, snapshot: cabc.Mapping[str, object], now: float, *, accepted: bool
    ) -> None:
        """Close the interval after an attempt, or defer a lost record.

        Parameters
        ----------
        snapshot : Mapping[str, object]
            The values the attempt carried.
        now : float
            Monotonic seconds at which the attempt ran.
        accepted : bool
            Whether the record reached its destination.
        """
        if not accepted:
            # Peaks and the integral stay folded, so the next record covers
            # both intervals; the spacing delay keeps a failing sink from being
            # retried on every tick.
            self._deferred_until = now + self.min_spacing
            return
        self.queued_seconds = 0.0
        self.queued_peak = _count(snapshot.get("queued"))
        self.held_peak = _count(snapshot.get("held"))
        self._flushed = now
        self._flushed_counts = {
            name: count
            for name in _STEP_FIELDS
            if (count := _count(snapshot.get(name))) is not None
        }
        self._stepped = False

    def _integrate(self, now: float) -> None:
        """Add the previous depth over the elapsed monotonic time."""
        if self._previous_time is None or self._previous_queued is None:
            return
        self.queued_seconds += self._previous_queued * max(
            0.0, now - self._previous_time
        )

    def _crossed_a_step(self, snapshot: cabc.Mapping[str, object]) -> bool:
        """Report whether a count moved by a whole step since the last record.

        Parameters
        ----------
        snapshot : Mapping[str, object]
            Current supervisor-wide values; missing fields cannot have moved.

        Returns
        -------
        bool
            Whether any stepped count is at least one whole step away from the
            value carried by the last delivered record.
        """
        for name in _STEP_FIELDS:
            sample = _count(snapshot.get(name))
            baseline = self._flushed_counts.get(name)
            if sample is None or baseline is None:
                continue
            if abs(sample - baseline) >= self.step:
                return True
        return False
