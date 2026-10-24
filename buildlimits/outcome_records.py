"""Pure cache-outcome compaction over the strict record format values."""

import dataclasses as dc
import datetime as dt
import typing as typ

from buildlimits.outcome_record_codec import (
    decode_lines as decode_lines,
)
from buildlimits.outcome_record_codec import (
    encode_line as encode_line,
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

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_vocabulary import StoreReason

MAX_COMPACT_BYTES = 12 * 1024 * 1024
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
    decoded: DecodedStore, *, now: dt.datetime, clock_skew: bool = False
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
    header = dc.replace(decoded.header, written_at=now)
    records = list(decoded.records)
    if clock_skew:
        reason: StoreReason = "clock_skew"
    else:
        cutoff = now - dt.timedelta(days=RETENTION_DAYS)
        records = [record for record in records if record.observed_at >= cutoff]
        reason = "expired" if len(records) < len(decoded.records) else "none"
    discarded = len(decoded.records) - len(records)
    while (
        len(records) > MAX_COMPACT_RECORDS
        or _serialized_size(header, records) > MAX_COMPACT_BYTES
    ):
        if not records:
            break
        records.pop(0)
        discarded += 1
    return Compacted(header, tuple(records), discarded, reason)


def _serialized_size(header: StoreHeader, records: cabc.Sequence[StoreRecord]) -> int:
    """Measure the replacement JSON Lines document including line feeds.

    Returns
    -------
    int
        Full byte count, including each row's terminating newline.
    """
    return sum(
        len(encode_line(value).encode("utf-8")) + 1 for value in (header, *records)
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
