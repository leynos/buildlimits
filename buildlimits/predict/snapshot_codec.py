"""Encode and decode the snapshot document and its event rows.

An event is a domain value: an identity, an origin, a label and the times those
became known. The JSON shapes are storage concerns, so the codec that reads and
writes them lives beside the snapshot store rather than on the values
themselves. This also keeps :mod:`buildlimits.predict.events` free of a JSON
dependency and keeps a domain value from carrying a serialization method.
"""

import hashlib
import json
import math
import typing as typ

from buildlimits.predict.events import LABELS, ORIGINS, Event
from buildlimits.utc_text import utc_moment, utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_record_format import Origin
    from buildlimits.predict.dataset import Gap, Snapshot, Source
    from buildlimits.predict.events import Label

SNAPSHOT_VERSION = 1
_EVENT_FIELDS = frozenset({
    "identity",
    "origin",
    "admitted_at",
    "observed_at",
    "label",
    "runtime_seconds",
})
_IDENTITY_LENGTH = 64


def event_to_dict(event: Event) -> dict[str, object]:
    """Return the stable snapshot shape for one event.

    Returns
    -------
    dict[str, object]
        Every field, with timestamps in canonical UTC text.
    """
    return {
        "identity": event.identity,
        "origin": event.origin,
        "admitted_at": _stamp(event.admitted_at),
        "observed_at": _stamp(event.observed_at),
        "label": event.label,
        "runtime_seconds": round(event.runtime_seconds, 6),
    }


def gap_to_dict(gap: Gap) -> dict[str, object]:
    """Return the snapshot shape for one reported silence.

    Returns
    -------
    dict[str, object]
        The gap under stable keys.
    """
    return {
        "start": utc_text(gap.start),
        "end": utc_text(gap.end),
        "seconds": round(gap.seconds, 3),
    }


def source_to_dict(source: Source) -> dict[str, object]:
    """Return the snapshot shape for the store provenance.

    Returns
    -------
    dict[str, object]
        The provenance under stable keys.
    """
    return {
        "kind": "cache-outcomes",
        "salt_id": source.salt_id,
        "written_at": utc_text(source.written_at),
        "records_total": source.records_total,
        "outcome_records": source.outcome_records,
        "summary_records": source.summary_records,
    }


def snapshot_to_dict(snapshot: Snapshot) -> dict[str, object]:
    """Return the complete versioned snapshot document.

    Returns
    -------
    dict[str, object]
        The document written to disk.
    """
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "created_at": utc_text(snapshot.created_at),
        "source": source_to_dict(snapshot.source),
        "journal": snapshot.journal,
        "stats": snapshot.stats(),
        "events": [event_to_dict(event) for event in snapshot.events],
    }


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
        line = json.dumps(event_to_dict(event), sort_keys=True, separators=(",", ":"))
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

    Text that is not an offset timestamp, or whose offset leaves the range a
    UTC datetime can hold, is refused as a ``ValueError`` by
    :func:`buildlimits.utc_text.utc_moment`.

    Returns
    -------
    datetime | None
        The parsed time, or None for null.

    Raises
    ------
    TypeError
        If the value is neither text nor null.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        msg = "a timestamp must be text or null"
        raise TypeError(msg)
    return utc_moment(value, label="a timestamp")
