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
    ObserverAccounting,
)
from buildlimits.utc_text import optional_utc_moment, optional_utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.predict.journal_provenance import JournalProvenance

_OBSERVER_COUNT_FIELDS = (
    "requests",
    "outcomes",
    "record_omitted",
    "record_omitted_unrecorded",
    "store_records",
    "store_discarded",
    "readings",
    "readings_skipped",
    "readings_failed",
)
_OBSERVER_MAP_FIELDS = (
    "by_cache_path",
    "by_backend",
    "by_result",
    "by_reason",
    "by_attribution",
    "by_origin",
    "by_omission_reason",
    "by_store_operation",
    "by_store_reason",
    "by_gate_reason",
    "by_reading_reason",
)


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
        "observer": (
            None
            if provenance.observer is None
            else _observer_to_dict(provenance.observer)
        ),
    }


def _observer_to_dict(observer: ObserverAccounting) -> dict[str, object]:
    """Return the snapshot shape for the recorder's own counts.

    Returns
    -------
    dict[str, object]
        Counts and their closed-vocabulary breakdowns.
    """
    return {
        **{name: getattr(observer, name) for name in _OBSERVER_COUNT_FIELDS},
        **{name: dict(getattr(observer, name)) for name in _OBSERVER_MAP_FIELDS},
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
        observer=_observer_from(note.get("observer"), label),
    )


def _observer_from(value: object, label: str) -> ObserverAccounting | None:
    """Decode the optional recorder-count block.

    A snapshot written before the block existed carries none. Absence is kept
    distinct from a measured zero: None means the evidence was not recorded,
    while a present block with zero counts means the queried window held none.

    Returns
    -------
    ObserverAccounting | None
        The validated counts, or None when the block is absent.

    Raises
    ------
    TypeError
        If the block or one of its values has the wrong Python type.
    ValueError
        If a count is negative, the store-reason breakdown does not sum to the
        discarded total it explains, or the omission-reason breakdown does not
        sum to the omitted total it explains.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        msg = f"{label} journal observer must be an object"
        raise TypeError(msg)
    block = typ.cast("dict[str, object]", value)
    expected = {*_OBSERVER_COUNT_FIELDS, *_OBSERVER_MAP_FIELDS}
    # A present block is a measurement, so every field the writer emits must be
    # there. Defaulting a missing field to zero would report an absent count as
    # a measured one, which is the error this block exists to prevent. The
    # observer block is part of this branch's unreleased snapshot format, so
    # requiring the omission fields refuses no document a release wrote.
    if set(block) != expected:
        msg = f"{label} journal observer must hold exactly the recorded fields"
        raise TypeError(msg)
    counts = {
        name: _journal_count(block[name], label, f"observer {name}")
        for name in _OBSERVER_COUNT_FIELDS
    }
    if any(count < 0 for count in counts.values()):
        msg = f"{label} journal observer counts must not be negative"
        raise ValueError(msg)
    maps = {
        name: _tally_from(block[name], label, f"observer {name}")
        for name in _OBSERVER_MAP_FIELDS
    }
    if sum(maps["by_store_reason"].values()) != counts["store_discarded"]:
        msg = f"{label} journal observer store reasons do not sum to its discards"
        raise ValueError(msg)
    if sum(maps["by_omission_reason"].values()) != counts["record_omitted"]:
        msg = f"{label} journal observer omissions do not sum to its omitted total"
        raise ValueError(msg)
    if (
        counts["record_omitted"] + counts["record_omitted_unrecorded"]
        > counts["outcomes"]
    ):
        msg = f"{label} journal observer omissions exceed its published outcomes"
        raise ValueError(msg)
    return ObserverAccounting.from_parts(counts, maps)


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
