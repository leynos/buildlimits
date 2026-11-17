"""One event per recorded compilation, in the order the store wrote it.

Events keep the store's append order. A batch is a maximal run of consecutive
records sharing one label time, so a backward clock step can order timestamps
against recording order without merging records the store wrote in separate
windows. Features are measured at the moment a request was admitted, when the
source recorded that time, and at the label time otherwise. Their history is
the same embargoed history the replay uses: eligible hit/miss labels from the
batches before the one immediately preceding the row's batch. An unknown
outcome carries no usable label, so it neither counts as load nor sets an age.
The feature set therefore describes what an admission decision could have seen,
and never an outcome the replay has not yet released.
"""

import bisect
import dataclasses as dc
import datetime as dt
import hashlib
import json
import typing as typ

from buildlimits.utc_text import utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_record_format import Origin

type Label = typ.Literal["hit", "miss", "unknown"]
type ScoredLabel = typ.Literal["hit", "miss"]

LABELS: tuple[Label, ...] = ("hit", "miss", "unknown")
ORIGINS: tuple[Origin, ...] = ("registry", "git", "path", "unknown")
_EVENT_FIELDS = frozenset({
    "identity",
    "origin",
    "admitted_at",
    "observed_at",
    "label",
    "runtime_seconds",
})
_IDENTITY_LENGTH = 64
_SCORED_LABELS: frozenset[Label] = frozenset({"hit", "miss"})
EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


@dc.dataclass(frozen=True, slots=True)
class Event:
    """One compilation's identity, label and when that label became known.

    Attributes
    ----------
    identity : str
        Salted, hashed compilation identity; no crate name or path is stored.
    origin : Origin
        The crate's origin class, which is known before admission.
    admitted_at : datetime | None
        When the compilation was admitted, if the source recorded it.
    observed_at : datetime
        When the label became known.
    label : Label
        ``hit`` or ``miss`` when attributed, otherwise ``unknown``.
    runtime_seconds : float
        The recorded compilation runtime, used only as a feature.
    """

    identity: str
    origin: Origin
    admitted_at: dt.datetime | None
    observed_at: dt.datetime
    label: Label
    runtime_seconds: float

    def to_dict(self) -> dict[str, object]:
        """Return the stable snapshot shape for one event.

        Returns
        -------
        dict[str, object]
            Every field, with timestamps in canonical UTC text.
        """
        return {
            "identity": self.identity,
            "origin": self.origin,
            "admitted_at": _stamp(self.admitted_at),
            "observed_at": _stamp(self.observed_at),
            "label": self.label,
            "runtime_seconds": round(self.runtime_seconds, 6),
        }


@dc.dataclass(frozen=True, slots=True)
class Features:
    """What a predictor may read before one event's label is known.

    Attributes
    ----------
    identity : str
        The hashed compilation identity.
    origin : Origin
        The crate's origin class.
    observed_at : datetime
        The prediction time, equal to the label-availability time.
    age_seconds : float | None
        Seconds since this identity's previous eligible label strictly before
        the moment the row is measured, or None when no earlier label was known
        then.
    load : int
        Eligible labels in the released history inside the trailing load
        window, excluding unknown outcomes, the embargoed batch, and anything
        labelled at or after the row's measurement moment.
    """

    identity: str
    origin: Origin
    observed_at: dt.datetime
    age_seconds: float | None
    load: int


def scored_label(event: Event) -> ScoredLabel | None:
    """Return the scorable label, or None when the outcome is unknown.

    Parameters
    ----------
    event : Event
        The event whose outcome is inspected.

    Returns
    -------
    ScoredLabel | None
        ``hit`` or ``miss`` for an attributed event, otherwise None.

    Examples
    --------
    >>> scored_label(Event("a" * 64, "git", None, EPOCH, "miss", 0.1))
    'miss'
    """
    label = event.label
    if label in _SCORED_LABELS:
        return typ.cast("ScoredLabel", label)
    return None


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


def fingerprint(events: cabc.Iterable[Event]) -> str:
    """Hash the canonical encoding of the event stream.

    Parameters
    ----------
    events : Iterable[Event]
        The events to hash, in the order they should contribute to the digest.

    Returns
    -------
    str
        A lowercase SHA-256 digest over every event in order.
    """
    digest = hashlib.sha256()
    for event in events:
        line = json.dumps(event.to_dict(), sort_keys=True, separators=(",", ":"))
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def event_from_dict(value: object) -> Event:
    """Validate one snapshot line as an event.

    Parameters
    ----------
    value : object
        One decoded JSON line, of any shape.

    Returns
    -------
    Event
        The decoded event.

    Raises
    ------
    TypeError
        If the line is not an object or a field holds the wrong Python type.
    ValueError
        If fields are missing or extra, or a value is outside the vocabulary.
    """
    if not isinstance(value, dict):
        msg = "an event line must be a JSON object"
        raise TypeError(msg)
    row = typ.cast("dict[str, object]", value)
    if set(row) != _EVENT_FIELDS:
        msg = "an event line must name exactly the snapshot fields"
        raise ValueError(msg)
    identity = row["identity"]
    origin = row["origin"]
    label = row["label"]
    runtime = row["runtime_seconds"]
    if not (isinstance(identity, str) and len(identity) == _IDENTITY_LENGTH):
        msg = "identity must be a 64-character digest"
        raise TypeError(msg)
    if origin not in ORIGINS or label not in LABELS:
        msg = "origin and label must use the accepted vocabulary"
        raise ValueError(msg)
    if not isinstance(runtime, (int, float)) or isinstance(runtime, bool):
        msg = "runtime_seconds must be numeric"
        raise TypeError(msg)
    observed = _parse_stamp(row["observed_at"])
    if observed is None:
        msg = "observed_at must be a UTC timestamp"
        raise ValueError(msg)
    return Event(
        identity=identity,
        origin=typ.cast("Origin", origin),
        admitted_at=_parse_stamp(row["admitted_at"]),
        observed_at=observed,
        label=typ.cast("Label", label),
        runtime_seconds=float(runtime),
    )


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


def _stamp(value: dt.datetime | None) -> str | None:
    """Serialize an optional UTC timestamp.

    Returns
    -------
    str | None
        Canonical ISO text, or None for an absent time.
    """
    return None if value is None else utc_text(value)


def _parse_stamp(value: object) -> dt.datetime | None:
    """Parse a canonical UTC timestamp or an explicit null.

    Returns
    -------
    datetime | None
        The parsed time, or None for null.

    Raises
    ------
    TypeError
        If the value is neither text nor null.
    ValueError
        If the text is not a valid timestamp with an offset.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        msg = "a timestamp must be text or null"
        raise TypeError(msg)
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as error:
        msg = "a timestamp must be ISO 8601"
        raise ValueError(msg) from error
    if parsed.tzinfo is None:
        msg = "a timestamp must carry an offset"
        raise ValueError(msg)
    return parsed.astimezone(dt.UTC)
