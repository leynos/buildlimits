"""Typed cache-outcome values and format-specific field validation."""

import dataclasses as dc
import datetime as dt
import math
import typing as typ

from buildlimits.json_values import nonnegative_integer
from buildlimits.outcome_key import CacheKey, ToolchainIdentity

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_parser import ParserClass
    from buildlimits.outcome_vocabulary import CacheResult

type Origin = typ.Literal["registry", "git", "path", "unknown"]
type ChildSighting = typ.Literal["seen", "ambiguous", "not_seen", "unavailable"]

FORMAT_NAME = "buildlimits-cache-outcomes"
FORMAT_VERSION = 1
MAX_RECORD_BYTES = 16 * 1024
MAX_STORE_BYTES = 16 * 1024 * 1024
_DIGEST_HEX_LENGTH = 64
_CACHE_RESULTS = frozenset([
    "hit",
    "miss",
    "not_cacheable",
    "not_cached",
    "failed",
    "error",
    "declined",
    "not_compile",
    "unsupported",
    "unattributed",
])
_ORIGINS = frozenset({"registry", "git", "path", "unknown"})
_CHILD_SIGHTINGS = frozenset({"seen", "ambiguous", "not_seen", "unavailable"})
_PARSER_CLASSES = frozenset({"cacheable", "not_compile", "declined"})


@dc.dataclass(frozen=True, slots=True)
class StoreHeader:
    """Store format identity and explicit write time."""

    salt_id: str
    written_at: dt.datetime


@dc.dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """One attributed request with bounded H2 and H3 evidence."""

    key: CacheKey
    outcome: CacheResult
    origin: Origin
    runtime_seconds: float
    observed_at: dt.datetime
    parser_class: ParserClass
    child_sighting: ChildSighting
    child_samples: int


@dc.dataclass(frozen=True, slots=True)
class WindowSummary:
    """One completed window's refusal deltas and predicted class counts."""

    observed_at: dt.datetime
    server_version: str
    window_groups: int
    requests_not_compile: int
    requests_not_cacheable: int
    predicted_not_compile: int
    predicted_declined: int
    predicted_cacheable: int


type StoreRecord = OutcomeRecord | WindowSummary


@dc.dataclass(frozen=True, slots=True)
class RecordFailure:
    """A store document rejected without exposing its contents."""

    reason: typ.Literal["corrupt", "version_mismatch"]


@dc.dataclass(frozen=True, slots=True)
class DecodedStore:
    """A validated header and records in append order."""

    header: StoreHeader
    records: tuple[StoreRecord, ...]


_KEY_FIELDS = {field.name for field in dc.fields(CacheKey)}
_IDENTITY_FIELDS = {field.name for field in dc.fields(ToolchainIdentity)}
_OUTCOME_FIELDS = {field.name for field in dc.fields(OutcomeRecord)} | {"kind"}
_SUMMARY_FIELDS = {field.name for field in dc.fields(WindowSummary)} | {"kind"}
_SUMMARY_COUNTS = tuple(field.name for field in dc.fields(WindowSummary)[2:])


def _valid_outcome(record: OutcomeRecord) -> bool:
    """Check closed vocabularies and bounded measurements.

    Returns
    -------
    bool
        Whether every persisted outcome field is valid.
    """
    return all((
        _is_choice(record.outcome, _CACHE_RESULTS),
        _is_choice(record.origin, _ORIGINS),
        _is_choice(record.parser_class, _PARSER_CLASSES),
        _is_choice(record.child_sighting, _CHILD_SIGHTINGS),
        _valid_number(record.runtime_seconds),
        nonnegative_integer(record.child_samples) is not None,
    ))


def _valid_outcome_fields(value: cabc.Mapping[str, object]) -> bool:
    """Check decoded outcome values against their closed vocabularies.

    Returns
    -------
    bool
        Whether all required outcome values have supported types and values.
    """
    return all((
        _is_choice(value.get("outcome"), _CACHE_RESULTS),
        _is_choice(value.get("origin"), _ORIGINS),
        _is_choice(value.get("parser_class"), _PARSER_CLASSES),
        _is_choice(value.get("child_sighting"), _CHILD_SIGHTINGS),
        _valid_number(value.get("runtime_seconds")),
        nonnegative_integer(value.get("child_samples")) is not None,
    ))


def _has_supported_version(value: cabc.Mapping[str, object]) -> bool:
    """Require the exact format name and integer version.

    Returns
    -------
    bool
        Whether the header identifies the supported format version.
    """
    version = value.get("version")
    return all((
        value.get("format") == FORMAT_NAME,
        isinstance(version, int),
        not isinstance(version, bool),
        version == FORMAT_VERSION,
    ))


def _validate_key(key: CacheKey) -> None:
    """Reject cache keys containing local paths or invalid digests.

    Raises
    ------
    ValueError
        If a key field is empty, malformed or path-bearing.
    """
    identity = key.toolchain_identity
    is_valid = all((
        _safe_component(key.crate_name),
        _safe_origin(key.crate_origin),
        _safe_target(key.target),
        _valid_digest(key.argument_digest) is not None,
        _valid_digest(identity.compiler_digest) is not None,
        _valid_digest(identity.rustup_toolchain_digest) is not None,
        _text(identity.cache_version) is not None,
        _text(identity.server_version) is not None,
    ))
    if not is_valid:
        raise ValueError


def _safe_component(value: object) -> bool:
    """Check a crate-name component without filesystem separators.

    Returns
    -------
    bool
        Whether the value is a non-empty, path-free string.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    return not any(separator in value for separator in ("/", "\\"))


def _safe_origin(value: object) -> bool:
    """Check a classified origin label without retaining path components.

    Returns
    -------
    bool
        Whether the optional origin is a supported path-free label.
    """
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    if not value.startswith(("registry:", "git:", "path:")):
        return False
    return not any(separator in value for separator in ("/", "\\"))


def _safe_target(value: object) -> bool:
    """Check a target triple or keyed target-path digest.

    Returns
    -------
    bool
        Whether the target is safe to persist.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    if value.startswith("path:"):
        return _valid_digest(value.removeprefix("path:")) is not None
    return not _path_like(value)


def _path_like(value: str) -> bool:
    """Identify Unix, relative and Windows path spellings.

    Returns
    -------
    bool
        Whether the value resembles a local filesystem path.
    """
    return any((
        any(separator in value for separator in ("/", "\\")),
        value.startswith((".", "~")),
        len(value) > 1 and value[1] == ":",
    ))


def _text(value: object) -> str | None:
    """Accept a non-empty string value.

    Returns
    -------
    str or None
        The original string, or no value for empty or non-string input.
    """
    return value if isinstance(value, str) and value.strip() else None


def _require_text(value: object) -> None:
    """Require one non-empty string field.

    Raises
    ------
    ValueError
        If the value is empty or is not a string.
    """
    if _text(value) is None:
        raise ValueError


def _is_choice(value: object, allowed: frozenset[str]) -> bool:
    """Check a string against a closed vocabulary.

    Returns
    -------
    bool
        Whether the string belongs to the allowed values.
    """
    return isinstance(value, str) and value in allowed


def _valid_number(value: object) -> bool:
    """Accept finite non-negative numeric measurements.

    Returns
    -------
    bool
        Whether the value is a finite non-negative number.
    """
    match value:
        case bool():
            return False
        case int() | float() as number:
            if number < 0:
                return False
            try:
                return math.isfinite(number)
            except OverflowError:
                return False
        case _:
            return False


def _valid_digest(value: object) -> str | None:
    """Accept lowercase SHA-256 hexadecimal digests.

    Returns
    -------
    str or None
        The digest, or no value for a malformed representation.
    """
    if not isinstance(value, str) or len(value) != _DIGEST_HEX_LENGTH:
        return None
    if not all(character in "0123456789abcdef" for character in value):
        return None
    return value


def _timestamp(value: dt.datetime) -> str:
    """Serialize an explicit UTC timestamp in canonical ISO form.

    Returns
    -------
    str
        The timestamp with a trailing ``Z`` offset.

    Raises
    ------
    ValueError
        If the timestamp is naive or has a non-UTC offset.
    """
    if value.tzinfo is None or value.utcoffset() != dt.timedelta(0):
        raise ValueError
    return value.isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> dt.datetime | None:
    """Parse an explicit UTC ISO timestamp.

    Returns
    -------
    datetime or None
        A UTC timestamp, or no value for malformed or non-UTC input.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        return None
    return parsed.astimezone(dt.UTC)
