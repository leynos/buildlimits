"""Strict JSON Lines decoding for cache-outcome store values."""

import typing as typ

from buildlimits.json_values import json_mapping as _mapping
from buildlimits.json_values import nonnegative_integer
from buildlimits.outcome_key import CacheKey, ToolchainIdentity
from buildlimits.outcome_record_format import (
    _ACCEPTED_OUTCOME_SHAPES,
    _IDENTITY_FIELDS,
    _KEY_FIELDS,
    _SUMMARY_COUNTS,
    _SUMMARY_FIELDS,
    _V1_OUTCOME_FIELDS,
    _V2_OUTCOME_FIELDS,
    _V3_OUTCOME_FIELDS,
    _V4_OUTCOME_FIELDS,
    FORMAT_NAME,
    FORMAT_VERSION,
    DecodedStore,
    OutcomeRecord,
    RecordFailure,
    StoreHeader,
    StoreRecord,
    WindowSummary,
    _has_supported_version,
    _text,
    _valid_outcome_fields,
)
from buildlimits.outcome_record_lines import (
    _parse_json_lines,
    _raw_json_row,
    _Unparsable,
)
from buildlimits.outcome_record_key import _validate_key
from buildlimits.outcome_record_time import (
    INVALID_TIMESTAMP,
    optional_timestamp,
    parse_timestamp,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_parser import ParserClass
    from buildlimits.outcome_record_format import (
        ChildSighting,
        LabelTimeProvenance,
        Origin,
    )
    from buildlimits.outcome_recorder_types import AttributionKind
    from buildlimits.outcome_vocabulary import CacheResult, OutcomeReason


def decode_header_line(line: bytes) -> StoreHeader | RecordFailure:
    """Decode one raw header row without a trailing newline.

    Parameters
    ----------
    line : bytes
        One candidate header line, with its newline already removed.

    Returns
    -------
    StoreHeader or RecordFailure
        The typed header, or a closed corruption or version reason.

    Examples
    --------
    >>> import datetime as dt
    >>> from buildlimits.outcome_record_encode import encode_line
    >>> raw = encode_line(StoreHeader("s", dt.datetime(2026, 1, 1, tzinfo=dt.UTC)))
    >>> decode_header_line(raw.encode()).salt_id
    's'
    """
    value = _raw_json_row(line)
    if isinstance(value, _Unparsable):
        return RecordFailure("corrupt")
    return _decode_header(value)


def decode_record_line(
    line: bytes, *, version: int = FORMAT_VERSION
) -> StoreRecord | RecordFailure:
    """Decode one raw non-header row without a trailing newline.

    Parameters
    ----------
    line : bytes
        One candidate record line, with its newline already removed.
    version : int
        The version of the document the row belongs to. The row shapes a
        document may hold differ by version, so a caller that has read its
        header must pass that version rather than let the default accept a
        current row that omits its provenance marker.

    Returns
    -------
    StoreRecord or RecordFailure
        The typed record, or a corruption reason.

    Examples
    --------
    >>> decode_record_line(b'{"kind":"unknown"}').reason
    'corrupt'
    """
    value = _raw_json_row(line)
    if isinstance(value, _Unparsable):
        return RecordFailure("corrupt")
    return _decode_record(value, version)


def decode_lines(document: str) -> DecodedStore | RecordFailure:
    r"""Decode bounded JSON Lines, ignoring only a malformed unterminated tail.

    Returns
    -------
    DecodedStore or RecordFailure
        Typed data, or a closed corruption/version reason.

    Examples
    --------
    >>> import datetime as dt
    >>> from buildlimits.outcome_record_encode import encode_line
    >>> header = StoreHeader("salt-1", dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    >>> decode_lines(encode_line(header) + chr(10)).header == header
    True
    """
    parsed = _parse_json_lines(document)
    if not parsed:
        return RecordFailure("corrupt")
    match _decode_header(parsed[0]):
        case RecordFailure() as failure:
            return failure
        case StoreHeader() as header:
            match _decode_records(parsed[1:], header.version):
                case RecordFailure() as failure:
                    return failure
                case records:
                    return DecodedStore(header, records)


def _decode_records(
    raw_records: cabc.Sequence[object], version: int
) -> tuple[StoreRecord, ...] | RecordFailure:
    """Decode the records after a valid header in append order.

    Returns
    -------
    tuple[StoreRecord, ...] or RecordFailure
        Decoded records, or the first rejected record reason.
    """
    records: list[StoreRecord] = []
    for raw in raw_records:
        match _decode_record(raw, version):
            case RecordFailure() as failure:
                return failure
            case (OutcomeRecord() | WindowSummary()) as record:
                records.append(record)
    return tuple(records)


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
    if value.get("format") != FORMAT_NAME:
        return RecordFailure("corrupt")
    if not _has_supported_version(value):
        return RecordFailure("version_mismatch")
    if set(value) != {"format", "version", "salt_id", "written_at"}:
        return RecordFailure("corrupt")
    salt_id = _text(value.get("salt_id"))
    written_at = parse_timestamp(value.get("written_at"))
    if salt_id is None or written_at is None:
        return RecordFailure("corrupt")
    # The file's own version travels with its header: a writer needs it to
    # know whether a current-format row may be appended beneath this one.
    return StoreHeader(salt_id, written_at, typ.cast("int", value["version"]))


def _decode_record(raw: object, version: int) -> StoreRecord | RecordFailure:
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
        return _decode_outcome(value, version)
    if value.get("kind") == "window_summary":
        return _decode_summary(value)
    return RecordFailure("corrupt")


def _decode_outcome(
    value: cabc.Mapping[str, object], version: int
) -> OutcomeRecord | RecordFailure:
    """Decode one validated outcome mapping.

A version 1 row has no admission or classification fields, and its absent
    evidence decodes as None. Neither an older row nor an older document carries
    the label-time provenance marker, the generation digest or the version 5
    correlation pair, so each decodes as None, which is unproven, unattributed
    and uncorrelated rather than false. A present but malformed timestamp,
    marker, digest or token is corruption, not an absent value.

    Returns
    -------
    OutcomeRecord or RecordFailure
        The typed outcome, or a corruption reason.
    """
    if frozenset(value) not in _ACCEPTED_OUTCOME_SHAPES.get(version, frozenset()):
        return RecordFailure("corrupt")
    key = _decode_key(value.get("key"))
    observed = parse_timestamp(value.get("observed_at"))
    admitted = optional_timestamp(value.get("admitted_at"))
    completed = optional_timestamp(value.get("completed_at"))
    if isinstance(key, RecordFailure) or observed is None:
        return RecordFailure("corrupt")
    if any(stamp is INVALID_TIMESTAMP for stamp in (admitted, completed)):
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
        typ.cast("OutcomeReason | None", value.get("outcome_reason")),
        typ.cast("AttributionKind | None", value.get("outcome_attribution")),
        typ.cast("dt.datetime | None", admitted),
typ.cast("LabelTimeProvenance | None", value.get("label_time_provenance")),
        typ.cast("str | None", value.get("generation_digest")),
        typ.cast("dt.datetime | None", completed),
        typ.cast("str | None", value.get("attempt")),
        typ.cast("str | None", value.get("supervisor_generation")),
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
    observed = parse_timestamp(value.get("observed_at"))
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
