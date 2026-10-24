"""Strict JSON Lines encoding and decoding for cache-outcome store values."""

import dataclasses as dc
import json
import typing as typ

from buildlimits.json_values import json_mapping as _mapping
from buildlimits.json_values import nonnegative_integer
from buildlimits.outcome_key import CacheKey, ToolchainIdentity
from buildlimits.outcome_record_format import (
    _IDENTITY_FIELDS,
    _KEY_FIELDS,
    _OUTCOME_FIELDS,
    _SUMMARY_COUNTS,
    _SUMMARY_FIELDS,
    FORMAT_NAME,
    FORMAT_VERSION,
    MAX_RECORD_BYTES,
    MAX_STORE_BYTES,
    DecodedStore,
    OutcomeRecord,
    RecordFailure,
    StoreHeader,
    StoreRecord,
    WindowSummary,
    _has_supported_version,
    _parse_timestamp,
    _require_text,
    _text,
    _timestamp,
    _valid_outcome,
    _valid_outcome_fields,
    _validate_key,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_parser import ParserClass
    from buildlimits.outcome_record_format import ChildSighting, Origin
    from buildlimits.outcome_vocabulary import CacheResult


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


def decode_lines(document: str) -> DecodedStore | RecordFailure:
    r"""Decode bounded JSON Lines, ignoring only a malformed unterminated tail.

    Returns
    -------
    DecodedStore or RecordFailure
        Typed data, or a closed corruption/version reason.

    Examples
    --------
    >>> import datetime as dt
    >>> header = StoreHeader("salt-1", dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    >>> decode_lines(encode_line(header) + chr(10)).header == header
    True
    """
    parsed = _parse_json_lines(document)
    if not parsed:
        return RecordFailure("corrupt")
    header = _decode_header(parsed[0])
    if isinstance(header, RecordFailure):
        return header
    records = _decode_records(parsed[1:])
    if isinstance(records, RecordFailure):
        return records
    return DecodedStore(header, records)


def _decode_records(
    raw_records: cabc.Sequence[object],
) -> tuple[StoreRecord, ...] | RecordFailure:
    """Decode the records after a valid header in append order.

    Returns
    -------
    tuple[StoreRecord, ...] or RecordFailure
        Decoded records, or the first rejected record reason.
    """
    records: list[StoreRecord] = []
    for raw in raw_records:
        record = _decode_record(raw)
        if isinstance(record, RecordFailure):
            return record
        records.append(record)
    return tuple(records)


def _parse_json_lines(document: str) -> tuple[object, ...] | None:
    """Parse bounded rows and drop a malformed final append fragment.

    Returns
    -------
    tuple[object, ...] or None
        Parsed rows, or no value for an invalid or oversized document.
    """
    if len(document.encode("utf-8")) > MAX_STORE_BYTES:
        return None
    lines = document.split("\n")
    terminated = document.endswith("\n")
    if terminated:
        lines.pop()
    if not lines or not lines[0]:
        return None
    parsed: list[object] = []
    for index, line in enumerate(lines):
        if not line or len(line.encode("utf-8")) > MAX_RECORD_BYTES:
            return None
        try:
            parsed.append(json.loads(line, parse_constant=lambda _value: None))
        except ValueError, TypeError, RecursionError:
            is_torn_tail = index == len(lines) - 1 and not terminated
            if is_torn_tail:
                break
            return None
    return tuple(parsed)


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


def _decode_header(raw: object) -> StoreHeader | RecordFailure:
    """Validate and decode the first JSON Lines header.

    Returns
    -------
    StoreHeader or RecordFailure
        The typed header, or a closed corruption or version reason.
    """
    value = _mapping(raw)
    if value is None:
        return RecordFailure("corrupt")
    if not _has_supported_version(value):
        return RecordFailure("version_mismatch")
    if set(value) != {"format", "version", "salt_id", "written_at"}:
        return RecordFailure("corrupt")
    salt_id = _text(value.get("salt_id"))
    written_at = _parse_timestamp(value.get("written_at"))
    if salt_id is None or written_at is None:
        return RecordFailure("corrupt")
    return StoreHeader(salt_id, written_at)


def _decode_record(raw: object) -> StoreRecord | RecordFailure:
    """Dispatch one record by its closed kind field.

    Returns
    -------
    StoreRecord or RecordFailure
        The typed record, or a corruption reason.
    """
    value = _mapping(raw)
    if value is None:
        return RecordFailure("corrupt")
    if value.get("kind") == "outcome":
        return _decode_outcome(value)
    if value.get("kind") == "window_summary":
        return _decode_summary(value)
    return RecordFailure("corrupt")


def _decode_outcome(value: cabc.Mapping[str, object]) -> OutcomeRecord | RecordFailure:
    """Decode one validated outcome mapping.

    Returns
    -------
    OutcomeRecord or RecordFailure
        The typed outcome, or a corruption reason.
    """
    if set(value) != _OUTCOME_FIELDS:
        return RecordFailure("corrupt")
    key = _decode_key(value.get("key"))
    observed = _parse_timestamp(value.get("observed_at"))
    if isinstance(key, RecordFailure) or observed is None:
        return RecordFailure("corrupt")
    if not _valid_outcome_fields(value):
        return RecordFailure("corrupt")
    return OutcomeRecord(
        key,
        typ.cast("CacheResult", value["outcome"]),
        typ.cast("Origin", value["origin"]),
        float(typ.cast("int | float", value["runtime_seconds"])),
        observed,
        typ.cast("ParserClass", value["parser_class"]),
        typ.cast("ChildSighting", value["child_sighting"]),
        typ.cast("int", value["child_samples"]),
    )


def _decode_summary(value: cabc.Mapping[str, object]) -> WindowSummary | RecordFailure:
    """Decode one validated window-summary mapping.

    Returns
    -------
    WindowSummary or RecordFailure
        The typed summary, or a corruption reason.
    """
    if set(value) != _SUMMARY_FIELDS:
        return RecordFailure("corrupt")
    observed = _parse_timestamp(value.get("observed_at"))
    version = _text(value.get("server_version"))
    if observed is None or version is None:
        return RecordFailure("corrupt")
    counts = tuple(value.get(name) for name in _SUMMARY_COUNTS)
    if not all(nonnegative_integer(count) is not None for count in counts):
        return RecordFailure("corrupt")
    return WindowSummary(observed, version, *typ.cast("tuple[int, ...]", counts))


def _decode_key(raw: object) -> CacheKey | RecordFailure:
    """Decode a path-free cache key and toolchain identity.

    Returns
    -------
    CacheKey or RecordFailure
        The typed key, or a corruption reason.
    """
    value = _mapping(raw)
    if value is None or set(value) != _KEY_FIELDS:
        return RecordFailure("corrupt")
    identity = _mapping(value.get("toolchain_identity"))
    if identity is None or set(identity) != _IDENTITY_FIELDS:
        return RecordFailure("corrupt")
    key = CacheKey(
        typ.cast("str", value.get("crate_name")),
        typ.cast("str | None", value.get("crate_origin")),
        typ.cast("str", value.get("target")),
        typ.cast("str", value.get("argument_digest")),
        ToolchainIdentity(
            typ.cast("str", identity.get("compiler_digest")),
            typ.cast("str", identity.get("rustup_toolchain_digest")),
            typ.cast("str", identity.get("cache_version")),
            typ.cast("str", identity.get("server_version")),
        ),
    )
    try:
        _validate_key(key)
    except ValueError:
        return RecordFailure("corrupt")
    return key
