"""One event per recorded compilation, in the order the store wrote it.

Events keep the store's append order, so a backward clock step can order
timestamps against recording order without moving a record into another
window. A prediction is made at the event's admission moment, and only a label
whose observation time is strictly earlier may inform it; that ordering lives
in :mod:`buildlimits.predict.history`. An unknown outcome carries no usable
label there, so it neither counts as load nor sets an age.
"""

import dataclasses as dc
import datetime as dt
import hashlib
import json
import math
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
        When the label became known.
    age_seconds : float | None
        Seconds from this identity's previous eligible label to the moment the
        row is measured, or None when no earlier label was known then.
    load : int
        Eligible labels inside the trailing load window that had arrived
        strictly before the measurement moment. An unknown outcome is never
        counted.
    predicted_at : datetime | None
        The admission moment the row is measured at, or None when the source
        cannot support a prediction. A row with no moment is never predicted.
    """

    identity: str
    origin: Origin
    observed_at: dt.datetime
    age_seconds: float | None
    load: int
    predicted_at: dt.datetime | None = None


def admission_moment(event: Event) -> dt.datetime | None:
    """Return the moment at which the event could have been predicted.

    A prediction needs an admission time, because that is when the decision is
    taken. A source that records none cannot support one: the label time is not
    a substitute, since assuming the label was already known then would be
    exactly the leakage the harness must prevent. An admission recorded after
    its own label describes a decision taken once the outcome was known, so it
    cannot support a prediction either. Both cases are reported as unavailable
    rather than repaired by guessing.

    Parameters
    ----------
    event : Event
        The recorded event.

    Returns
    -------
    datetime | None
        The admission time, or None when no usable one was recorded.

    Examples
    --------
    >>> admission_moment(Event("a" * 64, "git", None, EPOCH, "hit", 0.1)) is None
    True
    >>> admission_moment(
    ...     Event("a" * 64, "git", EPOCH, EPOCH, "hit", 0.1)
    ... ) == EPOCH
    True
    """
    admitted = event.admitted_at
    if admitted is None or admitted > event.observed_at:
        return None
    return admitted


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
    runtime_seconds = _runtime_seconds(runtime)
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
        runtime_seconds=runtime_seconds,
    )


def _runtime_seconds(value: object) -> float:
    """Validate the runtime recorded for one compilation.

    Returns
    -------
    float
        The runtime in seconds.

    Raises
    ------
    TypeError
        If the value is not a number.
    ValueError
        If the value is not finite, including an integer too large for a float
        to hold.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        msg = "runtime_seconds must be numeric"
        raise TypeError(msg)
    try:
        seconds = float(value)
    except OverflowError as error:
        # A JSON integer can be larger than a float can hold. That is a bad
        # field value like any other, not a crash in the snapshot loader.
        msg = "runtime_seconds must be finite"
        raise ValueError(msg) from error
    if not math.isfinite(seconds):
        msg = "runtime_seconds must be finite"
        raise ValueError(msg)
    return seconds


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
