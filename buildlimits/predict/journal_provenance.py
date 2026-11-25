"""Describe what the user journal can and cannot contribute.

The journal is the only source that can show the observer's own view of label
loss: how many requests were admitted, which cache path they took, which
outcomes the recorder published, and how many readings it refused or skipped.
It cannot join any of those counts to a store row, because neither source it
names carries a per-compilation identity. Admission times therefore come from
the outcome store, while the journal supplies bounded denominators and the
retention span.

Reading the journal is an adapter concern. The command that owns a journal
reader hands its records to :func:`count_journal`, so this module depends on
the fields it counts and never on the query that produced them.

The observer's own counts live in
:mod:`buildlimits.predict.observer_accounting`, because they measure a closed
vocabulary rather than describe the provenance note they travel with. They are
re-exported here so a caller that already holds a
:class:`~buildlimits.predict.journal_provenance.JournalCounts` needs one
import.
"""

import dataclasses as dc
import typing as typ

from buildlimits.predict.observer_accounting import (
    ObserverAccounting as ObserverAccounting,
)
from buildlimits.predict.observer_accounting import (
    observer_accounting as observer_accounting,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

__all__ = [
    "JournalCounts",
    "JournalProvenance",
    "JournalRecord",
    "JournalRestarts",
    "JournalUnavailable",
    "ObserverAccounting",
    "count_journal",
    "observer_accounting",
]

_CORRELATION_LIMIT = (
    "no joinable per-compilation admission record: adapter rows carry metrics "
    "without an identity and supervisor rows carry queue and outcome events "
    "without a compilation identity, so the journal supplies counts and a "
    "retention span while admission times come from the outcome store"
)


class JournalRecord(typ.Protocol):
    """The journal fields the provenance note counts.

    A journal query's record type satisfies this shape, so the counter needs
    no import from the journal reader. The fields are read-only properties so
    a frozen record satisfies the protocol: a plain attribute would promise
    the counter a write it never makes, which a frozen dataclass cannot keep.
    """

    @property
    def source(self) -> str:
        """The journal source that wrote the record."""
        ...

    @property
    def event(self) -> str:
        """The event name the record states."""
        ...

    @property
    def timestamp(self) -> dt.datetime | None:
        """When the record was written, or None when it carries no time."""
        ...

    @property
    def fields(self) -> cabc.Mapping[str, str]:
        """The validated, prefix-free schema fields the record carries."""
        ...


@dc.dataclass(frozen=True, slots=True)
class JournalRestarts:
    """The supervisor starts one journal window witnessed.

    Attributes
    ----------
    count : int
        Every ``supervisor_started`` event the window held.
    first : datetime | None
        The earliest dated start, or None when no start carried a time.
    last : datetime | None
        The latest dated start, or None when no start carried a time.
    """

    count: int
    first: dt.datetime | None = None
    last: dt.datetime | None = None


@dc.dataclass(frozen=True, slots=True)
class JournalCounts:
    """What a journal query could count, and what it could not join.

    Attributes
    ----------
    span : str
        The lookback the query was asked for.
    truncated : bool
        Whether the query hit its record cap.
    records : int
        Every record the query returned.
    span_start : datetime | None
        The earliest record time the window covers, or None when undated.
    span_end : datetime | None
        The latest record time the window covers, or None when undated.
    by_source : Mapping[str, int]
        Record counts per journal source.
    by_event : Mapping[str, int]
        Record counts per journal event.
    restarts : JournalRestarts
        The supervisor starts the window saw.
    retention : str
        How far back the query is reliable, in words.
    correlation : str
        The join the journal cannot support.
    observer : ObserverAccounting | None
        The recorder's own bounded counts from the same window, or None when
        an older snapshot carries no observer block. None is not a measured
        zero: it means the evidence was not recorded.
    """

    span: str
    truncated: bool
    records: int
    span_start: dt.datetime | None
    span_end: dt.datetime | None
    by_source: cabc.Mapping[str, int]
    by_event: cabc.Mapping[str, int]
    restarts: JournalRestarts
    retention: str
    correlation: str
    observer: ObserverAccounting | None = None

    @property
    def available(self) -> bool:
        """Whether the window held any record."""
        return bool(self.records)


@dc.dataclass(frozen=True, slots=True)
class JournalUnavailable:
    """Why the benchmark has no journal counts.

    Attributes
    ----------
    reason : str
        The reason the report states, such as a failed query.
    """

    reason: str


JournalProvenance = JournalCounts | JournalUnavailable


def count_journal(
    records: cabc.Iterable[JournalRecord],
    *,
    since: str,
    cap: int,
    truncated: bool,
) -> JournalCounts:
    """Describe what one journal window offers the benchmark.

    Parameters
    ----------
    records : Iterable[JournalRecord]
        The records the query returned, in its own order.
    since : str
        The lookback the query was asked for, such as ``"24h"``.
    cap : int
        The record cap the query applied, named in the retention note.
    truncated : bool
        Whether the query reached that cap.

    Returns
    -------
    JournalCounts
        Availability, counts by source and event, the supervisor starts the
        window saw, the retention the query covers and the correlation it
        cannot support.

    Examples
    --------
    >>> from buildlimits.metrics_records import Record
    >>> import datetime as dt
    >>> moment = dt.datetime(2026, 11, 17, 12, tzinfo=dt.UTC)
    >>> note = count_journal(
    ...     [Record("supervisor", "admission", {}, moment)],
    ...     since="24h",
    ...     cap=1000,
    ...     truncated=False,
    ... )
    >>> note.records, note.span_start.isoformat()
    (1, '2026-11-17T12:00:00+00:00')
    """
    window = tuple(records)
    by_source: dict[str, int] = {}
    by_event: dict[str, int] = {}
    for record in window:
        by_source[record.source] = by_source.get(record.source, 0) + 1
        by_event[record.event] = by_event.get(record.event, 0) + 1
    stamps = sorted(
        record.timestamp for record in window if record.timestamp is not None
    )
    starts = [
        record.timestamp for record in window if record.event == "supervisor_started"
    ]
    dated_starts = sorted(stamp for stamp in starts if stamp is not None)
    return JournalCounts(
        span=since,
        truncated=truncated,
        records=len(window),
        span_start=stamps[0] if stamps else None,
        span_end=stamps[-1] if stamps else None,
        by_source=dict(sorted(by_source.items())),
        by_event=dict(sorted(by_event.items())),
        restarts=JournalRestarts(
            count=len(starts),
            first=dated_starts[0] if dated_starts else None,
            last=dated_starts[-1] if dated_starts else None,
        ),
        retention=_retention_note(since, cap=cap, truncated=truncated),
        correlation=_CORRELATION_LIMIT,
        observer=observer_accounting(window),
    )


def _retention_note(since: str, *, cap: int, truncated: bool) -> str:
    """Describe how far back the journal query is reliable.

    Returns
    -------
    str
        The requested lookback, the record cap and whether the cap was hit.
    """
    note = (
        f"at most {cap} records over the requested {since} "
        "lookback; older records are outside what this query can see"
    )
    if truncated:
        note += ", and the cap was reached so the newest records won"
    return note
