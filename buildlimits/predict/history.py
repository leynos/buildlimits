"""The released history: label batches and the causal features they yield.

A batch is a maximal run of consecutive store records sharing one label time.
The replay withholds the batch immediately before the one it predicts, so a
row's history ends two batches back. Feature rows are measured on that same
history: a batch is released only after the following batch has been answered,
and only eligible hit/miss labels that arrived strictly before the measurement
moment contribute. An unknown outcome carries no usable label, so it neither
counts as load nor sets an age.
"""

import bisect
import datetime as dt
import typing as typ

from buildlimits.predict.events import Features, scored_label

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.predict.events import Event


def label_batches(events: cabc.Sequence[Event]) -> tuple[tuple[int, ...], ...]:
    """Group consecutive events whose labels were written together.

    Parameters
    ----------
    events : Sequence[Event]
        The events in store append order.

    Returns
    -------
    tuple[tuple[int, ...], ...]
        One index tuple per distinct label time, in stream order.
    """
    batches: list[tuple[int, ...]] = []
    start = 0
    for index in range(1, len(events) + 1):
        if index == len(events) or (
            events[index].observed_at != events[start].observed_at
        ):
            batches.append(tuple(range(start, index)))
            start = index
    return tuple(batches)


def features(
    events: cabc.Sequence[Event], *, load_window_seconds: float
) -> tuple[Features, ...]:
    """Derive each event's causal features from the history it may use.

    A row in one batch reads every eligible label from the batches before the
    one immediately preceding it, because the one-batch embargo withholds the
    previous batch from every prediction. Rows are measured within that
    history, so a label that arrived at or after the measurement moment still
    cannot contribute. Unknown outcomes still receive a row, so row alignment
    never changes, but they carry no label to learn from.

    Parameters
    ----------
    events : Sequence[Event]
        The events in store append order.
    load_window_seconds : float
        The trailing window, in seconds, over which load is counted.

    Returns
    -------
    tuple[Features, ...]
        One feature row per event, in the same order.

    Examples
    --------
    The first event of an identity has no age and no load:

    >>> from buildlimits.predict.events import EPOCH, Event
    >>> first = features((Event("a" * 64, "git", None, EPOCH, "hit", 0.1),),
    ...                  load_window_seconds=60.0)[0]
    >>> first.age_seconds is None and first.load == 0
    True
    """
    history: list[dt.datetime] = []
    per_identity: dict[str, list[dt.datetime]] = {}
    rows: list[Features | None] = [None] * len(events)
    pending: tuple[int, ...] | None = None
    for batch in label_batches(events):
        for index in batch:
            event = events[index]
            moment = _measurement_moment(event)
            previous = per_identity.get(event.identity, [])
            last = _last_label_before(previous, moment)
            rows[index] = Features(
                identity=event.identity,
                origin=event.origin,
                observed_at=event.observed_at,
                age_seconds=None if last is None else (moment - last).total_seconds(),
                load=_load_before(history, moment, load_window_seconds),
            )
        if pending is not None:
            _remember(history, per_identity, events, pending)
        pending = batch
    return tuple(typ.cast("Features", row) for row in rows)


def _remember(
    history: list[dt.datetime],
    per_identity: dict[str, list[dt.datetime]],
    events: cabc.Sequence[Event],
    batch: cabc.Sequence[int],
) -> None:
    """Add one batch's eligible labels to the feature history."""
    for index in batch:
        event = events[index]
        if scored_label(event) is None:
            continue
        bisect.insort(history, event.observed_at)
        bisect.insort(per_identity.setdefault(event.identity, []), event.observed_at)


def _measurement_moment(event: Event) -> dt.datetime:
    """Return the moment an event's features are measured at.

    A decision is made when the request is admitted, so that is the moment the
    row describes. A source that records no admission time falls back to the
    label time, and an admission recorded after its own label is treated the
    same way: the label cannot still be unknown after it arrived.

    Returns
    -------
    datetime
        The admission time, or the label time when admission is unknown.
    """
    admitted = event.admitted_at
    if admitted is None or admitted > event.observed_at:
        return event.observed_at
    return admitted


def _last_label_before(
    times: cabc.Sequence[dt.datetime], moment: dt.datetime
) -> dt.datetime | None:
    """Return the latest label time strictly before ``moment``, if any.

    A label whose time equals the measurement moment is not an earlier label:
    it may belong to a row of the same batch, whose labels are still embargoed
    when the row is predicted. Load is counted over the same half-open range,
    so an age and a load never disagree about a tied instant.

    Returns
    -------
    datetime | None
        The latest known label time, or None when none is early enough.
    """
    index = bisect.bisect_left(times, moment) - 1
    return None if index < 0 else times[index]


def _load_before(
    times: cabc.Sequence[dt.datetime], moment: dt.datetime, window_seconds: float
) -> int:
    """Count earlier eligible labels inside the trailing load window.

    The window ends strictly before ``moment``, so an event labelled at the
    same moment is not load, and a zero-second window counts nothing.
    ``times`` must be sorted and hold eligible labels from the released
    history only, as :func:`features` leaves it.

    Returns
    -------
    int
        Events with a label time in ``[moment - window, moment)``.
    """
    window = dt.timedelta(seconds=window_seconds)
    return bisect.bisect_left(times, moment) - bisect.bisect_left(
        times, moment - window
    )
