"""Deterministic JSON Lines encoding for cache-outcome store values.

Encoding is separated from decoding so the writer's hot path imports only the
format constants and validators it needs. The output is compact, ordered JSON
with no trailing newline, which is what the append path and the archiver both
expect.
"""

import dataclasses as dc
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
    _timestamp,
    _valid_outcome,
    _validate_key,
)

__all__ = ["encode_line"]


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
    '{"format":"buildlimits-cache-outcomes","version":1,"salt_id":"salt-1","written_at":"2026-01-01T00:00:00Z"}'
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
                "written_at": _timestamp(value.written_at),
            }
        case OutcomeRecord() as record:
            _validate_key(record.key)
            if not _valid_outcome(record):
                raise ValueError
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
    """Map one validated record and canonicalize its observation time.

    Returns
    -------
    dict[str, object]
        JSON-compatible fields prefixed with the record kind.
    """
    fields = dc.asdict(record)
    fields["observed_at"] = _timestamp(record.observed_at)
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
