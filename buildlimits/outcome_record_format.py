"""Typed cache-outcome values and format-specific field validation."""

import dataclasses as dc
import datetime as dt
import math
import typing as typ

from buildlimits.json_values import nonnegative_integer
from buildlimits.metrics_schema import CACHE_RESULTS as _CACHE_RESULTS
from buildlimits.metrics_schema import OUTCOME_REASONS as _OUTCOME_REASONS
from buildlimits.outcome_key import CacheKey, ToolchainIdentity
from buildlimits.outcome_record_key import (
    _DIGEST_HEX_LENGTH as _DIGEST_HEX_LENGTH,
)
from buildlimits.outcome_record_key import (
    _is_choice as _is_choice,
)
from buildlimits.outcome_record_key import (
    _optional_choice as _optional_choice,
)
from buildlimits.outcome_record_key import (
    _optional_digest as _optional_digest,
)
from buildlimits.outcome_record_key import (
    _text as _text,
)
from buildlimits.outcome_record_key import (
    _valid_digest as _valid_digest,
)
from buildlimits.outcome_record_key import (
    _validate_key as _validate_key,
)
from buildlimits.outcome_record_timing import (
    valid_evidence as _valid_timing_evidence,
    valid_fields as _valid_timing_fields,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_parser import ParserClass
    from buildlimits.outcome_recorder_types import AttributionKind
    from buildlimits.outcome_vocabulary import CacheResult, OutcomeReason

type Origin = typ.Literal["registry", "git", "path", "unknown"]
type ChildSighting = typ.Literal["seen", "ambiguous", "not_seen", "unavailable"]
type LabelTimeProvenance = typ.Literal["verified"]

FORMAT_NAME = "buildlimits-cache-outcomes"
FORMAT_VERSION = 5
SUPPORTED_FORMAT_VERSIONS = frozenset({1, 2, 3, 4, FORMAT_VERSION})
MAX_RECORD_BYTES = 16 * 1024
MAX_STORE_BYTES = 16 * 1024 * 1024
# The largest document an in-place compaction may write. A policy that lets
# the active file grow past this bound cannot compact it in legacy mode, so
# the retention policy refuses such a segment size.
MAX_COMPACT_BYTES = 12 * 1024 * 1024
_ORIGINS = frozenset({"registry", "git", "path", "unknown"})
_CHILD_SIGHTINGS = frozenset({"seen", "ambiguous", "not_seen", "unavailable"})
_PARSER_CLASSES = frozenset({"cacheable", "not_compile", "declined"})
_ATTRIBUTIONS = frozenset({"solo", "unanimous", "inferred", "none"})
_PROVENANCES = frozenset({"verified"})


@dc.dataclass(frozen=True, slots=True)
class StoreHeader:
    """Store format identity, write time and the file's own format version.

    ``version`` is the version the document was read at or will be written
    with. It defaults to the current format, and decoding preserves whatever
    the file itself carried, so a writer can tell that the file it loaded is
    older than the rows it is about to append and settle it first.
    """

    salt_id: str
    written_at: dt.datetime
    version: int = FORMAT_VERSION


@dc.dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """One attributed request with bounded evidence and its timing provenance.

    Attributes
    ----------
    key : CacheKey
        The salted request identity.
    outcome : CacheResult
        The closed cache-result vocabulary.
    origin : Origin
        The crate's origin class.
    runtime_seconds : float
        The recorded compilation runtime.
    observed_at : datetime
        When the label became known, as the source recorded it. A version 1 or
        2 row carries the completion-derived time the old recorder chose, which
        ``label_time_provenance`` marks as unproven. The current recorder dates
        every row at the statistics reading that produced its label, so for a
        verified row this is also the moment its label became available.
    parser_class : ParserClass
        The argv parser's predicted class.
    child_sighting : ChildSighting
        The bounded child-process observation.
    child_samples : int
        Successful child samples taken while the launch ran.
    outcome_reason : OutcomeReason | None
        Why the window produced no label, or ``none`` for a label. ``None``
        means the row predates reason recording and is never guessed at.
    outcome_attribution : AttributionKind | None
        ``solo``, ``unanimous`` or ``none``; ``None`` for a pre-v2 row.
    admitted_at : datetime | None
        The UTC wall-clock moment the supervisor admitted the compilation,
        captured where the admission decision is applied. ``None`` means the
        row predates admission-time recording or the launch was not admitted
        through the supervisor.
    label_time_provenance : Literal["verified"] | None
        Whether ``observed_at`` is proven to be when the label became known.
        The current recorder writes ``verified``, because it dates every row at
        the statistics reading that produced the label. ``None`` means the row
        predates the marker (a version 1 or 2 row) and proves nothing: either
        release it as a lower bound or withhold it, never treat it as proof.
    generation_digest : str | None
        A salted digest naming the sccache server generation the recording
        reading observed. ``None`` means no single generation can be attributed
        to this row: it predates the field, its window changed or lost the
        server identity, or the row was emitted without a reading at all. It is
        a correlation key only, so the digest separates label loss per
        generation without ever naming a process.
    completed_at : datetime | None
        The UTC wall-clock moment the launch's group completion was handled.
        ``None`` when the row predates completion recording or the group had
        not been observed completing when the row was written. It is the
        launch's own moment, kept apart from the reading that dated the label.
    attempt : str | None
        The bounded admission group identifier for this one invocation. A
        cache key recurs across compilations, so this is what tells two
        concurrent or retried invocations of one key apart. ``None`` for a
        version 1 to 4 row.
    supervisor_generation : str | None
        The bounded identifier of the supervisor process whose clock stamped
        the row. It changes on every supervisor start, so ``attempt`` values
        from different processes never collide. ``None`` for a pre-v5 row.
    """

    key: CacheKey
    outcome: CacheResult
    origin: Origin
    runtime_seconds: float
    observed_at: dt.datetime
    parser_class: ParserClass
    child_sighting: ChildSighting
    child_samples: int
    outcome_reason: OutcomeReason | None = None
    outcome_attribution: AttributionKind | None = None
    admitted_at: dt.datetime | None = None
    label_time_provenance: LabelTimeProvenance | None = None
    generation_digest: str | None = None
    completed_at: dt.datetime | None = None
    attempt: str | None = None
    supervisor_generation: str | None = None


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
    """A validated header and records in append order.

    Attributes
    ----------
    header : StoreHeader
        The newest header represented.
    records : tuple[StoreRecord, ...]
        Every record, in append order.
    salt_ids : tuple[str, ...]
        Every record domain the history spans, oldest first. Empty for a store
        read from a single file, where ``header.salt_id`` is the only domain.
    """

    header: StoreHeader
    records: tuple[StoreRecord, ...]
    salt_ids: tuple[str, ...] = ()


_KEY_FIELDS = {field.name for field in dc.fields(CacheKey)}
_IDENTITY_FIELDS = {field.name for field in dc.fields(ToolchainIdentity)}
_OUTCOME_FIELDS = {field.name for field in dc.fields(OutcomeRecord)} | {"kind"}
# Version 5 adds the per-attempt correlation group and the completion moment.
# Version 4 rows predate that group; version 3 rows also predate the generation
# digest; version 2 rows also predate the label-time provenance marker; version
# 1 rows also predate admission-time and classification evidence. All stay
# readable, and every absent field decodes as None rather than a guessed
# generation, proof, correlation or label.
_TIMING_FIELDS = frozenset({"completed_at", "attempt", "supervisor_generation"})
_V4_OUTCOME_FIELDS = _OUTCOME_FIELDS - _TIMING_FIELDS
_V3_OUTCOME_FIELDS = _V4_OUTCOME_FIELDS - {"generation_digest"}
_V2_OUTCOME_FIELDS = _V3_OUTCOME_FIELDS - {"label_time_provenance"}
_V1_OUTCOME_FIELDS = _V2_OUTCOME_FIELDS - {
    "outcome_reason",
    "outcome_attribution",
    "admitted_at",
}
# The outcome shapes each document version may hold. A current document holds
# only the current shape, so its rows state their provenance: the writer always
# emits the marker, and a row preserved from an older document is re-encoded by
# compaction with an explicit null rather than losing the field. An older
# document keeps the shapes its release wrote, plus the older rows a previous
# compaction of that release preserved.
_ACCEPTED_OUTCOME_SHAPES: dict[int, frozenset[frozenset[str]]] = {
    1: frozenset({frozenset(_V1_OUTCOME_FIELDS)}),
    2: frozenset({frozenset(_V1_OUTCOME_FIELDS), frozenset(_V2_OUTCOME_FIELDS)}),
    3: frozenset({frozenset(_V2_OUTCOME_FIELDS), frozenset(_V3_OUTCOME_FIELDS)}),
    4: frozenset({frozenset(_V3_OUTCOME_FIELDS), frozenset(_V4_OUTCOME_FIELDS)}),
    FORMAT_VERSION: frozenset({frozenset(_OUTCOME_FIELDS)}),
}
_SUMMARY_FIELDS = {field.name for field in dc.fields(WindowSummary)} | {"kind"}
_SUMMARY_COUNTS = tuple(field.name for field in dc.fields(WindowSummary)[2:])


def _valid_outcome(record: OutcomeRecord) -> bool:
    """Check closed vocabularies, bounded measurements and optional evidence.

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
        _optional_choice(record.outcome_reason, _OUTCOME_REASONS),
        _optional_choice(record.outcome_attribution, _ATTRIBUTIONS),
        _optional_utc(record.admitted_at),
        _optional_choice(record.label_time_provenance, _PROVENANCES),
        _optional_digest(record.generation_digest),
        _optional_utc(record.completed_at),
        _valid_timing_evidence(record),
    ))






def _valid_outcome_fields(value: cabc.Mapping[str, object]) -> bool:
    """Check decoded outcome values against their closed vocabularies.

    The optional evidence is absent from a version 1 row, which is valid: its
    fields decode as None and are never filled by inference.

    Returns
    -------
    bool
        Whether all present outcome values have supported types and values.
    """
    return all((
        _is_choice(value.get("outcome"), _CACHE_RESULTS),
        _is_choice(value.get("origin"), _ORIGINS),
        _is_choice(value.get("parser_class"), _PARSER_CLASSES),
        _is_choice(value.get("child_sighting"), _CHILD_SIGHTINGS),
        _valid_number(value.get("runtime_seconds")),
        nonnegative_integer(value.get("child_samples")) is not None,
        _optional_choice(value.get("outcome_reason"), _OUTCOME_REASONS),
        _optional_choice(value.get("outcome_attribution"), _ATTRIBUTIONS),
        _optional_choice(value.get("label_time_provenance"), _PROVENANCES),
        _optional_digest(value.get("generation_digest")),
        _valid_timing_fields(value),
    ))




def _has_supported_version(value: cabc.Mapping[str, object]) -> bool:
    """Require the supported format name and one readable integer version.

    The version is checked before the set lookup. A list or object version is
    not hashable, and constructing a tuple of checks would evaluate the lookup
    before ``all`` could short-circuit, turning malformed input into a crash.

    Returns
    -------
    bool
        Whether the header identifies a supported format version.
    """
    if value.get("format") != FORMAT_NAME:
        return False
    version = value.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        return False
    return version in SUPPORTED_FORMAT_VERSIONS


def _require_text(value: object) -> None:
    """Require one non-empty string field.

    Raises
    ------
    ValueError
        If the value is empty or is not a string.
    """
    if _text(value) is None:
        raise ValueError


def _optional_utc(value: dt.datetime | None) -> bool:
    """Check an optional timestamp is present in explicit UTC.

    Returns
    -------
    bool
        Whether the value is None or an aware UTC datetime.
    """
    return value is None or (
        value.tzinfo is not None and value.utcoffset() == dt.timedelta(0)
    )


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
