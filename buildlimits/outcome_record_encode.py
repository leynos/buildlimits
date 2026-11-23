"""Deterministic JSON Lines encoding for cache-outcome store values.

Encoding is separated from decoding so the writer's hot path imports only the
format constants and validators it needs. The output is compact, ordered JSON
with no trailing newline, which is what the append path and the archiver both
expect.
"""

import dataclasses as dc
import datetime as dt
import json

from buildlimits.json_values import nonnegative_integer
from buildlimits.outcome_record_format import (
    FORMAT_NAME,
    FORMAT_VERSION,
    MAX_RECORD_BYTES,
    OutcomeRecord,
    StoreHeader,
    StoreRecord,
    WindowSummary,
    _require_text,
    _valid_outcome,
    _validate_key,
)
from buildlimits.outcome_record_time import timestamp_text

__all__ = ["encode_line", "header_floor_bytes"]


def encode_line(value: StoreHeader | StoreRecord) -> str:
    """Encode one store value as deterministic compact JSON.

    Returns
    -------
    str
        One JSON object without a trailing newline.

    Raises
    ------
    ValueError
        If a field is invalid or the encoded row exceeds 16 KiB.

    Examples
    --------
    >>> import datetime as dt
    >>> encode_line(StoreHeader("salt-1", dt.datetime(2026, 1, 1, tzinfo=dt.UTC)))
    '{"format":"buildlimits-cache-outcomes","version":2,"salt_id":"salt-1","written_at":"2026-01-01T00:00:00Z"}'
    """
    document = _document(value)
    line = json.dumps(
        document, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    if len(line.encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValueError
    return line


def _document(value: StoreHeader | StoreRecord) -> dict[str, object]:
    """Validate and map one record dataclass in stable field order.

    Returns
    -------
    dict[str, object]
        A JSON-compatible mapping with a stable field order.

    Raises
    ------
    TypeError
        If ``value`` is not a supported store value.
    ValueError
        If a field value or timestamp is invalid.
    """
    match value:
        case StoreHeader():
            _require_text(value.salt_id)
            return {
                "format": FORMAT_NAME,
                "version": FORMAT_VERSION,
                "salt_id": value.salt_id,
                "written_at": timestamp_text(value.written_at),
            }
        case OutcomeRecord() as record:
            _validate_key(record.key)
            if not _valid_outcome(record):
                msg = "outcome record fields are invalid"
                raise ValueError(msg)
            return _record_document(record, "outcome")
        case WindowSummary() as summary:
            _require_text(summary.server_version)
            counts = _summary_counts(summary)
            if not all(nonnegative_integer(count) is not None for count in counts):
                raise ValueError
            return _record_document(summary, "window_summary")
        case _:
            raise TypeError


def _record_document(record: StoreRecord, kind: str) -> dict[str, object]:
    """Map one validated record and canonicalize its timestamps.

    An absent optional timestamp is written as an explicit JSON null, so a row
    states that no admission moment is known rather than omitting the field.

    Returns
    -------
    dict[str, object]
        JSON-compatible fields prefixed with the record kind.
    """
    fields = dc.asdict(record)
    fields["observed_at"] = timestamp_text(record.observed_at)
    if isinstance(record, OutcomeRecord):
        fields["admitted_at"] = (
            None if record.admitted_at is None else timestamp_text(record.admitted_at)
        )
    return {"kind": kind, **fields}


def _summary_counts(summary: WindowSummary) -> tuple[int, ...]:
    """Return the six persisted count fields in schema order.

    Returns
    -------
    tuple[int, ...]
        Counts in the order used by the window-summary JSON object.
    """
    return (
        summary.window_groups,
        summary.requests_not_compile,
        summary.requests_not_cacheable,
        summary.predicted_not_compile,
        summary.predicted_declined,
        summary.predicted_cacheable,
    )


def header_floor_bytes() -> int:
    """Return the smallest possible compacted document: one maximal header.

    A compacted document always starts with a replacement header, so no
    reachable store is smaller than one. The timestamp carries microseconds
    because the encoder preserves fractional seconds: a header written at a
    whole second is shorter than one written with any fraction, and a budget
    sized to the shorter form would still be unable to hold the longer one.

    Returns
    -------
    int
        The encoded size of a header with the longest salt identifier and a
        fractional-second timestamp, plus its newline. A positive byte budget
        below this cannot be honoured.

    Examples
    --------
    >>> header_floor_bytes() > 0
    True
    """
    written_at = dt.datetime(1970, 1, 1, microsecond=1, tzinfo=dt.UTC)
    header = StoreHeader("0" * 64, written_at)
    return len(encode_line(header).encode("utf-8")) + 1
