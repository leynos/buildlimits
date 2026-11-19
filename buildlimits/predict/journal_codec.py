"""Encode and decode the snapshot's journal provenance block.

The journal supplies counts and a retention span, never a compilation
identity, so its provenance is a small value of its own. It is kept apart from
the event rows because it is decoded by different rules: an empty block means
"no journal was queried", while a counts block is validated field by field
before the report prints any of it.
"""

import typing as typ

from buildlimits.predict.journal_provenance import (
    JournalCounts,
    JournalRestarts,
    JournalUnavailable,
)
from buildlimits.utc_text import optional_utc_moment, optional_utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.predict.journal_provenance import JournalProvenance


def journal_to_dict(provenance: JournalProvenance | None) -> dict[str, object]:
    """Return the snapshot shape for the journal provenance.

    Returns
    -------
    dict[str, object]
        The counts, or the reason none are available; an empty mapping when
        no journal was queried.
    """
    if provenance is None:
        return {}
    if isinstance(provenance, JournalUnavailable):
        return {"available": False, "reason": provenance.reason}
    return {
        "available": provenance.available,
        "span": provenance.span,
        "truncated": provenance.truncated,
        "records": provenance.records,
        "span_start": optional_utc_text(provenance.span_start),
        "span_end": optional_utc_text(provenance.span_end),
        "by_source": dict(provenance.by_source),
        "by_event": dict(provenance.by_event),
        "restarts": {
            "count": provenance.restarts.count,
            "first": optional_utc_text(provenance.restarts.first),
            "last": optional_utc_text(provenance.restarts.last),
        },
        "retention": provenance.retention,
        "correlation": provenance.correlation,
    }


def journal_from_dict(value: object, *, label: str) -> JournalProvenance | None:
    """Decode the journal provenance block.

    The report reads availability, the printed record count, the reason when
    unavailable, the retention span and the supervisor-start block, so a
    hand-edited document cannot smuggle a value the Markdown formatter would
    print wrongly. An empty block is accepted: it means the snapshot target
    queried no journal.

    Parameters
    ----------
    value : object
        The decoded ``journal`` value, of any shape.
    label : str
        What the caller calls the document, used to name it in an error.

    Returns
    -------
    JournalProvenance | None
        The counts, the reason none are available, or None for no query.

    A negative count or a timestamp that cannot be read is refused as a
    ``ValueError`` by the helper that decodes the counts.

    Raises
    ------
    TypeError
        If the block or a field it reads has the wrong type, or a counts block
        omits a field the report prints.
    """
    if not isinstance(value, dict):
        msg = f"{label} journal must be an object"
        raise TypeError(msg)
    note = typ.cast("dict[str, object]", value)
    if not note:
        return None
    available = note.get("available")
    if not isinstance(available, bool):
        msg = f"{label} journal must hold a boolean available"
        raise TypeError(msg)
    if not available and "span" not in note:
        return JournalUnavailable(_reason_from(note, label))
    return _counts_from(note, label)


def _counts_from(note: cabc.Mapping[str, object], label: str) -> JournalCounts:
    """Decode the counts block of a journal note that queried records.

    An empty window is a counts block too: it records a span and zero records,
    so it stays distinguishable from a query that was never made.

    Returns
    -------
    JournalCounts
        The validated counts.

    Raises
    ------
    TypeError
        If a field the report prints has the wrong type.
    ValueError
        If a count is negative or a timestamp cannot be read.
    """
    records = _journal_count(note.get("records"), label, "records")
    if records < 0:
        msg = f"{label} journal records must not be negative"
        raise ValueError(msg)
    restarts = _restarts_from(note.get("restarts"), label)
    span = note.get("span")
    truncated = note.get("truncated")
    retention = note.get("retention")
    correlation = note.get("correlation")
    if not isinstance(span, str):
        msg = f"{label} journal must hold span text"
        raise TypeError(msg)
    if not isinstance(truncated, bool):
        msg = f"{label} journal must hold a boolean truncated"
        raise TypeError(msg)
    if not isinstance(retention, str) or not isinstance(correlation, str):
        msg = f"{label} journal must hold retention and correlation text"
        raise TypeError(msg)
    return JournalCounts(
        span=span,
        truncated=truncated,
        records=records,
        span_start=_optional_stamp(note.get("span_start"), label, "span_start"),
        span_end=_optional_stamp(note.get("span_end"), label, "span_end"),
        by_source=_tally_from(note.get("by_source"), label, "by_source"),
        by_event=_tally_from(note.get("by_event"), label, "by_event"),
        restarts=restarts,
        retention=retention,
        correlation=correlation,
    )


def _reason_from(note: cabc.Mapping[str, object], label: str) -> str:
    """Return the reason an unavailable note states.

    Returns
    -------
    str
        The stated reason, or the default when the note states none.

    Raises
    ------
    TypeError
        If the reason is neither text nor null.
    """
    reason = note.get("reason")
    if reason is None:
        return "no records"
    if not isinstance(reason, str):
        msg = f"{label} journal reason must be text"
        raise TypeError(msg)
    return reason


def _journal_count(value: object, label: str, name: str) -> int:
    """Return one count the report would print as a number.

    Returns
    -------
    int
        The count, which the caller checks for a negative value.

    Raises
    ------
    TypeError
        If the value is not an integer.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{label} journal {name} must be an integer"
        raise TypeError(msg)
    return value


def _restarts_from(value: object, label: str) -> JournalRestarts:
    """Decode the supervisor-start block the report renders.

    A non-null count with a ``first`` bound also selects the bounded-range
    wording, which reads ``last``; without it the renderer would fail with a
    missing key instead of naming the field.

    Returns
    -------
    JournalRestarts
        The decoded starts.

    Raises
    ------
    TypeError
        If the block has the wrong type, a field has the wrong type, or a
        bounded range omits its last bound.
    ValueError
        If the count is negative or a bound cannot be read.
    """
    if not isinstance(value, dict):
        msg = f"{label} journal restarts must be an object"
        raise TypeError(msg)
    block = typ.cast("dict[str, object]", value)
    count = _journal_count(block.get("count"), label, "restarts count")
    if count < 0:
        msg = f"{label} journal restarts count must not be negative"
        raise ValueError(msg)
    first = _optional_stamp(block.get("first"), label, "restarts first")
    last = _optional_stamp(block.get("last"), label, "restarts last")
    if first is not None and last is None:
        msg = f"{label} journal restarts must hold a last bound with first"
        raise TypeError(msg)
    return JournalRestarts(count=count, first=first, last=last)


def _optional_stamp(value: object, label: str, name: str) -> dt.datetime | None:
    """Decode one optional timestamp the journal block carries.

    A present value that is not text is refused as a ``TypeError``, and text
    that is not an offset timestamp, or whose offset leaves the range a UTC
    datetime can hold, is refused as a ``ValueError`` by
    :func:`buildlimits.utc_text.utc_moment`.

    Returns
    -------
    datetime | None
        The moment, or None for an absent bound.
    """
    described = f"{label} journal {name}"
    return optional_utc_moment(
        value, label=described, message=f"{described} must be text"
    )


def _tally_from(value: object, label: str, name: str) -> dict[str, int]:
    """Decode one per-key record tally.

    Returns
    -------
    dict[str, int]
        The counts, under the keys the snapshot recorded.

    Raises
    ------
    TypeError
        If the tally or one of its counts has the wrong type.
    ValueError
        If a count is negative.
    """
    if not isinstance(value, dict):
        msg = f"{label} journal must hold a {name} object"
        raise TypeError(msg)
    tally: dict[str, int] = {}
    for key, count in typ.cast("dict[str, object]", value).items():
        if isinstance(count, bool) or not isinstance(count, int):
            msg = f"{label} journal {name} must hold integer counts"
            raise TypeError(msg)
        if count < 0:
            msg = f"{label} journal {name} must not hold negative counts"
            raise ValueError(msg)
        tally[key] = count
    return tally
