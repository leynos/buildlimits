"""The causal history: prediction moments and the features they may read.

Every prediction is made at the event's admission moment, never when its batch
happens to be processed. A label is released only once its own observation time
has passed: at a prediction moment the history holds exactly the eligible
labels recorded strictly earlier. Events sharing an admission moment are
predicted together, so a label observed at that same moment cannot inform any
of them. An event without a usable admission time is not predicted at all; a
missing timestamp is never read as if its label had arrived before it.

An unknown outcome carries no usable label, so it neither counts as load nor
sets an age.

Grouping by admission moment is only meaningful while the recorded times share
one clock. A clock that stepped backwards would let a label observed later in
true time carry an earlier stamp, so the schedule would release it before a
moment that truly preceded it. The snapshot loader refuses an admission-bearing
history whose observation times decrease in append order, before any of these
functions see it; a reset is reported rather than scored.
"""

import bisect
import datetime as dt
import typing as typ

from buildlimits.predict.events import Features, admission_moment, scored_label

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.predict.events import Event


def label_batches(events: cabc.Sequence[Event]) -> tuple[tuple[int, ...], ...]:
    """Group consecutive events whose labels were written together.

    The tuning/held-out split is placed between batches rather than inside one,
    so every row a store wrote at one label time lands in the same phase.

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


def prediction_moments(events: cabc.Sequence[Event]) -> tuple[tuple[int, ...], ...]:
    """Group the predictable events by admission moment, earliest first.

    Events sharing a moment are kept in store order, so a tie is answered in a
    fixed sequence. Events without a usable admission time are absent: they
    cannot be predicted. Sorting by moment relies on the stream's clock not
    having run backwards; the snapshot loader refuses such a history before it
    reaches this function.

    Parameters
    ----------
    events : Sequence[Event]
        The events in store append order.

    Returns
    -------
    tuple[tuple[int, ...], ...]
        One index tuple per distinct admission moment, in ascending time order.

    Examples
    --------
    >>> from buildlimits.predict.events import EPOCH, Event
    >>> label = EPOCH + dt.timedelta(seconds=5)
    >>> outcome = Event("a" * 64, "git", EPOCH, label, "hit", 0.1)
    >>> prediction_moments((outcome,))
    ((0,),)
    >>> known_first = Event("a" * 64, "git", label, EPOCH, "hit", 0.1)
    >>> prediction_moments((known_first,))
    ()
    """
    grouped: dict[dt.datetime, list[int]] = {}
    for index, event in enumerate(events):
        moment = admission_moment(event)
        if moment is not None:
            grouped.setdefault(moment, []).append(index)
    return tuple(tuple(indices) for _, indices in sorted(grouped.items()))


def admission_schedule(
    events: cabc.Sequence[Event],
    moments: cabc.Sequence[tuple[int, ...]],
    released: cabc.Sequence[tuple[dt.datetime, int]],
) -> cabc.Iterator[tuple[tuple[int, ...], tuple[int, ...]]]:
    """Yield each admission moment with the labels released before it.

    The labels are those observed strictly earlier than the moment, in release
    order, so a label observed at the moment itself waits for a later one. Both
    the single-predictor walk and the ensemble walk answer through this
    schedule, so neither can drift into reading a label it should not.

    Parameters
    ----------
    events : Sequence[Event]
        The events in store append order.
    moments : Sequence[tuple[int, ...]]
        The admission moments to answer, from :func:`prediction_moments`.
    released : Sequence[tuple[datetime, int]]
        Every eligible label in release order, from :func:`released_labels`.

    Yields
    ------
    tuple[tuple[int, ...], tuple[int, ...]]
        The stream indices whose labels are now due, then the moment's own.

    Examples
    --------
    >>> from buildlimits.predict.events import EPOCH, Event
    >>> first = Event("a" * 64, "git", EPOCH, EPOCH, "hit", 0.1)
    >>> stream = (first,)
    >>> tuple(admission_schedule(stream, prediction_moments(stream),
    ...                          released_labels(stream)))
    (((), (0,)),)
    """
    pointer = 0
    for group in moments:
        moment = typ.cast("dt.datetime", admission_moment(events[group[0]]))
        start = pointer
        while pointer < len(released) and released[pointer][0] < moment:
            pointer += 1
        yield (tuple(index for _, index in released[start:pointer]), group)


def features(
    events: cabc.Sequence[Event], *, load_window_seconds: float
) -> tuple[Features | None, ...]:
    """Derive each predictable event's row from the history it may use.

    A row is measured at its own admission moment and reads only eligible
    labels observed strictly earlier. Age is measured back to this identity's
    previous such label, and load counts every eligible label in the trailing
    window ending at the moment. An event without a usable admission time gets
    no row: inventing a measurement moment for it would be the leak this
    function exists to prevent.

    Parameters
    ----------
    events : Sequence[Event]
        The events in store append order.
    load_window_seconds : float
        The trailing window, in seconds, over which load is counted.

    Returns
    -------
    tuple[Features | None, ...]
        One row per event in the same order, or None where none is possible.

    Examples
    --------
    The first event of an identity has no age and no load:

    >>> from buildlimits.predict.events import EPOCH, Event
    >>> first = features(
    ...     (Event("a" * 64, "git", EPOCH, EPOCH, "hit", 0.1),),
    ...     load_window_seconds=60.0,
    ... )[0]
    >>> first is not None and first.age_seconds is None and first.load == 0
    True
    """
    stream = tuple(events)
    rows: list[Features | None] = [None] * len(stream)
    released = released_labels(stream)
    history: list[dt.datetime] = []
    per_identity: dict[str, list[dt.datetime]] = {}
    pointer = 0
    for group in prediction_moments(stream):
        moment = typ.cast("dt.datetime", admission_moment(stream[group[0]]))
        while pointer < len(released) and released[pointer][0] < moment:
            observed, index = released[pointer]
            bisect.insort(history, observed)
            bisect.insort(per_identity.setdefault(stream[index].identity, []), observed)
            pointer += 1
        for index in group:
            event = stream[index]
            previous = per_identity.get(event.identity, [])
            rows[index] = Features(
                identity=event.identity,
                origin=event.origin,
                observed_at=event.observed_at,
                age_seconds=(
                    None if not previous else (moment - previous[-1]).total_seconds()
                ),
                load=_load_before(history, moment, load_window_seconds),
                predicted_at=moment,
            )
    return tuple(rows)


def released_labels(events: cabc.Sequence[Event]) -> list[tuple[dt.datetime, int]]:
    """Return every label a predictor may ever learn, in release order.

    Only an event that could itself be predicted supplies a training row: a
    label from an event with no admission time has no pre-admission feature row
    to learn from, and manufacturing one would put a fabricated measurement
    into the model. Labels are ordered by observation time and then by store
    position, so an update order is always reproducible.

    Parameters
    ----------
    events : Sequence[Event]
        The events in store append order.

    Returns
    -------
    list[tuple[datetime, int]]
        Observed time and stream index of each eligible label.

    Examples
    --------
    >>> from buildlimits.predict.events import EPOCH, Event
    >>> released_labels((Event("a" * 64, "git", None, EPOCH, "hit", 0.1),))
    []
    """
    return sorted(
        (event.observed_at, index)
        for index, event in enumerate(events)
        if admission_moment(event) is not None and scored_label(event) is not None
    )


def _load_before(
    times: cabc.Sequence[dt.datetime], moment: dt.datetime, window_seconds: float
) -> int:
    """Count earlier eligible labels inside the trailing load window.

    The window ends strictly before ``moment``, so an event labelled at the
    same moment is not load, and a zero-second window counts nothing.
    ``times`` must be sorted and hold the labels released before ``moment``, as
    :func:`features` leaves it. A window that reaches before the earliest
    representable moment is clamped to that boundary, since nothing can have
    been observed earlier and subtracting it would raise rather than count.

    Returns
    -------
    int
        Events with a label time in ``[moment - window, moment)``.
    """
    window = dt.timedelta(seconds=window_seconds)
    try:
        earliest = moment - window
    except OverflowError:
        # A year-one admission time has no moment a positive window can reach
        # back to. Clamping to the minimum keeps the count at zero instead of
        # failing the whole stream on the earliest representable timestamp.
        earliest = dt.datetime.min.replace(tzinfo=dt.UTC)
    return bisect.bisect_left(times, moment) - bisect.bisect_left(times, earliest)
