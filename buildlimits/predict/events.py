"""One chronological event per recorded compilation, and its known features.

The benchmark orders events by the moment their label became known. Features
are measured at the moment a request was admitted, when the source recorded
that time, and at the label time otherwise; only eligible hit/miss labels that
were already known at that moment can contribute. An unknown outcome is a
record with no usable label, so it neither counts as load nor sets an age. The
feature set therefore describes what an admission decision could have seen,
and never a label that had not yet arrived.
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
        Seconds since this identity's previous eligible label, counted from the
        moment the row is measured, or None when no earlier label was known
        then.
    load : int
        Previously labelled events inside the trailing load window, excluding
        this one, unknown outcomes, and anything labelled after the row's
        measurement moment.
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


def ordered(events: cabc.Iterable[Event]) -> tuple[Event, ...]:
    """Sort events by label time, keeping append order for equal times.

    Parameters
    ----------
    events : Iterable[Event]
        The events in whatever order they were appended.

    Returns
    -------
    tuple[Event, ...]
        The events in chronological order.
    """
    return tuple(
        event
        for _, event in sorted(
            enumerate(events), key=lambda pair: (pair[1].observed_at, pair[0])
        )
    )


def features(
    events: cabc.Sequence[Event], *, load_window_seconds: float
) -> tuple[Features, ...]:
    """Derive each event's causal features from earlier eligible labels.

    Unknown outcomes still receive a row, so row alignment never changes, but
    they carry no label to learn from and are left out of the history.

    Parameters
    ----------
    events : Sequence[Event]
        The events sorted by label time, as :func:`ordered` leaves them.
        Bisecting on that order is what limits each row to eligible labels
        that had already arrived when it is measured.
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
    times = [event.observed_at for event in events if scored_label(event) is not None]
    seen: dict[str, list[dt.datetime]] = {}
    rows: list[Features] = []
    for event in events:
        moment = _measurement_moment(event)
        previous = seen.setdefault(event.identity, [])
        last = _last_label_before(previous, moment)
        rows.append(
            Features(
                identity=event.identity,
                origin=event.origin,
                observed_at=event.observed_at,
                age_seconds=None if last is None else (moment - last).total_seconds(),
                load=_load_before(times, moment, load_window_seconds),
            )
        )
        if scored_label(event) is not None:
            previous.append(event.observed_at)
    return tuple(rows)


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
    """Return the latest label time at or before ``moment``, if any.

    Returns
    -------
    datetime | None
        The latest known label time, or None when none is early enough.
    """
    index = bisect.bisect_right(times, moment) - 1
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
    ``times`` must be sorted and hold eligible labels only, as :func:`ordered`
    and :func:`features` leave it.

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
