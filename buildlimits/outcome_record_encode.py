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
    SUPPORTED_FORMAT_VERSIONS,
    OutcomeRecord,
    StoreHeader,
    StoreRecord,
    WindowSummary,
    _require_text,
    _valid_outcome,
    _validate_key,
)
from buildlimits.outcome_record_time import timestamp_text
from buildlimits.outcome_record_timing import TIMING_FIELDS

__all__ = ["encode_line", "header_floor_bytes"]

# The fields each document version's outcome row carries. A row is encoded in
# the shape its document version accepts, so a caller that writes an older
# document gets a faithful older row rather than a current one its own reader
# would refuse. The current shape is therefore not imposed on history.
_V2_ROW_FIELDS = frozenset({
    "key",
    "outcome",
    "origin",
    "runtime_seconds",
    "observed_at",
    "parser_class",
    "child_sighting",
    "child_samples",
    "outcome_reason",
    "outcome_attribution",
    "admitted_at",
})
_V1_ROW_FIELDS = _V2_ROW_FIELDS - {
    "outcome_reason",
    "outcome_attribution",
    "admitted_at",
}
_V3_ROW_FIELDS = _V2_ROW_FIELDS | {"label_time_provenance"}
_V4_ROW_FIELDS = _V3_ROW_FIELDS | {"generation_digest"}
_V5_ROW_FIELDS = _V4_ROW_FIELDS | TIMING_FIELDS
_ROW_FIELDS_BY_VERSION: dict[int, frozenset[str]] = {
    1: _V1_ROW_FIELDS,
    2: _V2_ROW_FIELDS,
    3: _V3_ROW_FIELDS,
    4: _V4_ROW_FIELDS,
    FORMAT_VERSION: _V5_ROW_FIELDS,
}
# The first document version that can state each optional proof. A row that
# carries one cannot be written beneath an older header, because that shape has
# nowhere to state it and dropping it would turn proof into silence.
_FIELD_INTRODUCED: dict[str, int] = {
    "label_time_provenance": 3,
    "generation_digest": 4,
    "completed_at": FORMAT_VERSION,
    "attempt": FORMAT_VERSION,
    "supervisor_generation": FORMAT_VERSION,
}


def encode_line(
    value: StoreHeader | StoreRecord, *, version: int = FORMAT_VERSION
) -> str:
    """Encode one store value as deterministic compact JSON.

    Parameters
    ----------
    value : StoreHeader | StoreRecord
        The typed store value to encode.
    version : int
        The document version the row is written into. It fixes the row shape,
        so writing a current row beneath an older header is refused rather than
        producing a document that version's own reader would call corrupt. A
        header carries its own version and ignores this argument.

    Returns
    -------
    str
        One JSON object without a trailing newline.

    Raises
    ------
    ValueError
        If a field is invalid, the value cannot be expressed at that version,
        or the encoded row exceeds 16 KiB.

    Examples
    --------
    >>> import datetime as dt
    >>> encode_line(StoreHeader("salt-1", dt.datetime(2026, 1, 1, tzinfo=dt.UTC)))
    '{"format":"buildlimits-cache-outcomes","version":4,"salt_id":"salt-1","written_at":"2026-01-01T00:00:00Z"}'
    """
    document = _document(value, version)
    line = json.dumps(
        document, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    if len(line.encode("utf-8")) > MAX_RECORD_BYTES:
        msg = "the encoded row exceeds the record byte bound"
        raise ValueError(msg)
    return line


def _document(value: StoreHeader | StoreRecord, version: int) -> dict[str, object]:
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
        If a field value or timestamp is invalid, or the row cannot be written
        at the requested document version.
    """
    match value:
        case StoreHeader():
            _require_text(value.salt_id)
            if isinstance(value.version, bool) or (
                value.version not in SUPPORTED_FORMAT_VERSIONS
            ):
                msg = "store header version is not supported"
                raise ValueError(msg)
            return {
                "format": FORMAT_NAME,
                "version": value.version,
                "salt_id": value.salt_id,
                "written_at": timestamp_text(value.written_at),
            }
        case OutcomeRecord() as record:
            _validate_key(record.key)
            if not _valid_outcome(record):
                msg = "outcome record fields are invalid"
                raise ValueError(msg)
            return _record_document(record, "outcome", version)
        case WindowSummary() as summary:
            _require_text(summary.server_version)
            counts = _summary_counts(summary)
            if not all(nonnegative_integer(count) is not None for count in counts):
                raise ValueError
            return _record_document(summary, "window_summary", version)
        case _:
            raise TypeError


def _record_document(record: StoreRecord, kind: str, version: int) -> dict[str, object]:
    """Map one validated record and canonicalize its timestamps.

    An absent optional timestamp is written as an explicit JSON null, so a row
    states that no admission moment is known rather than omitting the field. A
    document predating a field drops it, and an outcome row that has already
    claimed that evidence cannot be written into such a document: the older
    shape has nowhere to state it, and dropping it would turn proof into
    silence. That covers the label-time provenance marker, the generation
    digest, and the version 5 correlation pair with its completion moment.

    Returns
    -------
    dict[str, object]
        JSON-compatible fields prefixed with the record kind.

    Raises
    ------
    ValueError
        If the row states evidence its document version cannot carry.
    """
    fields = dc.asdict(record)
    fields["observed_at"] = timestamp_text(record.observed_at)
    if isinstance(record, OutcomeRecord):
        fields["admitted_at"] = (
            None if record.admitted_at is None else timestamp_text(record.admitted_at)
        )
        if record.completed_at is not None:
            fields["completed_at"] = timestamp_text(record.completed_at)
        for name, introduced in _FIELD_INTRODUCED.items():
            if version < introduced and getattr(record, name) is not None:
                msg = f"an older document cannot state {name.replace('_', ' ')}"
                raise ValueError(msg)
        allowed = _ROW_FIELDS_BY_VERSION.get(version)
        if allowed is None:
            msg = "store record version is not supported"
            raise ValueError(msg)
        fields = {name: value for name, value in fields.items() if name in allowed}
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
