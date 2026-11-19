"""Pure cache-outcome compaction over the strict record format values."""

import dataclasses as dc
import datetime as dt
import typing as typ

from buildlimits.outcome_record_codec import (
    decode_lines as decode_lines,
)
from buildlimits.outcome_record_encode import (
    encode_line as encode_line,
)
from buildlimits.outcome_record_format import (
    MAX_COMPACT_BYTES as MAX_COMPACT_BYTES,
)
from buildlimits.outcome_record_format import (
    MAX_RECORD_BYTES as MAX_RECORD_BYTES,
)
from buildlimits.outcome_record_format import (
    MAX_STORE_BYTES as MAX_STORE_BYTES,
)
from buildlimits.outcome_record_format import (
    ChildSighting as ChildSighting,
)
from buildlimits.outcome_record_format import (
    DecodedStore as DecodedStore,
)
from buildlimits.outcome_record_format import (
    Origin as Origin,
)
from buildlimits.outcome_record_format import (
    OutcomeRecord as OutcomeRecord,
)
from buildlimits.outcome_record_format import (
    RecordFailure as RecordFailure,
)
from buildlimits.outcome_record_format import (
    StoreHeader as StoreHeader,
)
from buildlimits.outcome_record_format import (
    StoreRecord as StoreRecord,
)
from buildlimits.outcome_record_format import (
    WindowSummary as WindowSummary,
)
from buildlimits.outcome_retention import RetentionPolicy

if typ.TYPE_CHECKING:
    from buildlimits.outcome_vocabulary import StoreReason

MAX_COMPACT_RECORDS = 10_000
RETENTION_DAYS = 30

__all__ = [
    "MAX_COMPACT_BYTES",
    "MAX_COMPACT_RECORDS",
    "MAX_RECORD_BYTES",
    "MAX_STORE_BYTES",
    "RETENTION_DAYS",
    "ChildSighting",
    "Compacted",
    "DecodedStore",
    "Origin",
    "OutcomeRecord",
    "RecordFailure",
    "StoreHeader",
    "StoreRecord",
    "WindowSummary",
    "compact",
    "decode_lines",
    "encode_line",
]


@dc.dataclass(frozen=True, slots=True)
class Compacted:
    """A bounded replacement header, records and discard count."""

    header: StoreHeader
    records: tuple[StoreRecord, ...]
    discarded: int
    store_reason: StoreReason


def compact(
    decoded: DecodedStore,
    *,
    now: dt.datetime,
    clock_skew: bool = False,
    policy: RetentionPolicy | None = None,
) -> Compacted:
    """Apply retention and size limits using the caller's explicit time.

    Parameters
    ----------
    decoded : DecodedStore
        Validated records in original append order.
    now : datetime
        Caller-supplied UTC wall time; this function reads no clock.
    clock_skew : bool, default=False
        Whether the caller detected a wall-clock jump exceeding one day.
    policy : RetentionPolicy | None, default=None
        The effective policy; the legacy defaults apply when omitted.

    Returns
    -------
    Compacted
        Replacement data with age, count and byte limits applied.

    Examples
    --------
    >>> header = StoreHeader("salt-1", dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    >>> compact(DecodedStore(header, ()), now=header.written_at).discarded
    0
    """
    _require_utc(now)
    active = _legacy_policy() if policy is None else policy
    header = dc.replace(decoded.header, written_at=now)
    records = list(decoded.records)
    if clock_skew:
        reason: StoreReason = "clock_skew"
    else:
        cutoff = active.cutoff(now)
        if cutoff is not None:
            records = [record for record in records if record.observed_at >= cutoff]
        reason = "expired" if len(records) < len(decoded.records) else "none"
    discarded = len(decoded.records) - len(records)
    record_sizes = [len(encode_line(record).encode("utf-8")) + 1 for record in records]
    total_size = len(encode_line(header).encode("utf-8")) + 1 + sum(record_sizes)
    first_retained = 0
    while first_retained < len(records) and _exceeds_compaction_limits(
        first_retained, len(records), total_size, active
    ):
        total_size -= record_sizes[first_retained]
        first_retained += 1
    discarded += first_retained
    return Compacted(header, tuple(records[first_retained:]), discarded, reason)


def _legacy_policy() -> RetentionPolicy:
    """Build the pre-segmentation policy from this module's public bounds.

    The module constants remain the documented legacy limits, so a caller
    that adjusts them still sees the adjustment take effect.

    Returns
    -------
    RetentionPolicy
        The age, byte and record bounds used when no policy is supplied.

    Examples
    --------
    >>> policy = _legacy_policy()
    >>> policy.max_bytes == MAX_COMPACT_BYTES
    True
    """
    return RetentionPolicy(
        max_age=dt.timedelta(days=RETENTION_DAYS),
        max_bytes=MAX_COMPACT_BYTES,
        max_records=MAX_COMPACT_RECORDS,
    )


def _exceeds_compaction_limits(
    first_retained: int,
    record_count: int,
    total_size: int,
    policy: RetentionPolicy,
) -> bool:
    """Check whether a store still exceeds its count or byte budget.

    Parameters
    ----------
    first_retained : int
        Leading records already discarded.
    record_count : int
        Records before this compaction pass.
    total_size : int
        Encoded bytes of the header and remaining records.
    policy : RetentionPolicy
        The bounds to test.

    Returns
    -------
    bool
        Whether one more leading record must be discarded.

    Examples
    --------
    >>> _exceeds_compaction_limits(0, MAX_COMPACT_RECORDS + 1, 0, RetentionPolicy())
    True
    """
    return policy.exceeds(
        logical_bytes=total_size, records=record_count - first_retained
    )


def _require_utc(value: dt.datetime) -> None:
    """Require explicit UTC time values; never read the system clock.

    Raises
    ------
    ValueError
        If the value is naive or has a non-UTC offset.
    """
    if value.tzinfo is None or value.utcoffset() != dt.timedelta(0):
        raise ValueError
