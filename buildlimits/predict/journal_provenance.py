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
"""

import collections
import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

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
class ObserverAccounting:
    """Bounded counts from the recorder's own journal events.

    Every map comes from a closed vocabulary. A count is present only when the
    event that carries it was in the queried window, so a truncated journal is
    reported as truncated rather than read as a zero.

    Attributes
    ----------
    requests : int
        ``request_admitted`` events the window held.
    by_cache_path : Mapping[str, int]
        Admitted requests by ``cached``, ``direct`` or ``direct_fallback``.
    by_backend : Mapping[str, int]
        Admitted requests by cache backend name.
    outcomes : int
        ``cache_outcome`` events the window held.
    by_result : Mapping[str, int]
        Published outcomes by cache result.
    by_reason : Mapping[str, int]
        Published outcomes by refusal reason.
    by_attribution : Mapping[str, int]
        Published outcomes by attribution kind.
    by_origin : Mapping[str, int]
        Published outcomes by crate origin.
    store_records : int
        Records the writer reported appended.
    store_discarded : int
        Records the writer reported dropped or evicted.
    readings : int
        Completed statistics readings.
    readings_skipped : int
        Due gates the recorder did not read at.
    readings_failed : int
        Readings that ended without a label.
    by_gate_reason : Mapping[str, int]
        Gate transitions by the clause that held the gate closed.
    by_reading_reason : Mapping[str, int]
        Reading transitions by their outcome reason.
    """

    requests: int = 0
    by_cache_path: cabc.Mapping[str, int] = dc.field(default_factory=dict)
    by_backend: cabc.Mapping[str, int] = dc.field(default_factory=dict)
    outcomes: int = 0
    by_result: cabc.Mapping[str, int] = dc.field(default_factory=dict)
    by_reason: cabc.Mapping[str, int] = dc.field(default_factory=dict)
    by_attribution: cabc.Mapping[str, int] = dc.field(default_factory=dict)
    by_origin: cabc.Mapping[str, int] = dc.field(default_factory=dict)
    store_records: int = 0
    store_discarded: int = 0
    readings: int = 0
    readings_skipped: int = 0
    readings_failed: int = 0
    by_gate_reason: cabc.Mapping[str, int] = dc.field(default_factory=dict)
    by_reading_reason: cabc.Mapping[str, int] = dc.field(default_factory=dict)


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
    observer : ObserverAccounting
        The recorder's own bounded counts from the same window.
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
    observer: ObserverAccounting = dc.field(default_factory=ObserverAccounting)

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


def observer_accounting(  # ruff: ignore[too-many-locals] - one accumulator per closed vocabulary.
    records: cabc.Iterable[JournalRecord],
) -> ObserverAccounting:
    """Count the recorder's own bounded events in one journal window.

    The maps are summed from the closed vocabularies the events already carry.
    A periodic ``outcome_reading`` summary repeats cumulative counters, so its
    counters are summed while its transition reasons are counted separately;
    the two grains never double-count each other.

    Parameters
    ----------
    records : Iterable[JournalRecord]
        The validated records the query returned.

    Returns
    -------
    ObserverAccounting
        Exact counts for the events the window held.
    """
    by_cache_path: collections.Counter[str] = collections.Counter()
    by_backend: collections.Counter[str] = collections.Counter()
    by_result: collections.Counter[str] = collections.Counter()
    by_reason: collections.Counter[str] = collections.Counter()
    by_attribution: collections.Counter[str] = collections.Counter()
    by_origin: collections.Counter[str] = collections.Counter()
    by_gate: collections.Counter[str] = collections.Counter()
    by_reading: collections.Counter[str] = collections.Counter()
    requests = outcomes = store_records = store_discarded = 0
    readings = skipped = failed = 0
    for record in records:
        fields = record.fields
        match record.event:
            case "request_admitted":
                requests += 1
                _count(by_cache_path, fields.get("cache_path"))
                _count(by_backend, fields.get("cache_backend"))
            case "cache_outcome":
                outcomes += 1
                _count(by_result, fields.get("cache_result"))
                _count(by_reason, fields.get("outcome_reason"))
                _count(by_attribution, fields.get("outcome_attribution"))
                _count(by_origin, fields.get("crate_origin"))
            case "outcome_store":
                store_records += _integer(fields.get("store_records"))
                store_discarded += _integer(fields.get("store_discarded"))
            case "outcome_reading":
                readings += _integer(fields.get("readings"))
                skipped += _integer(fields.get("readings_skipped"))
                failed += _integer(fields.get("readings_failed"))
                _count(by_gate, fields.get("gate_reason"))
                _count(by_reading, fields.get("outcome_reason"))
            case _:
                continue
    return ObserverAccounting(
        requests=requests,
        by_cache_path=dict(sorted(by_cache_path.items())),
        by_backend=dict(sorted(by_backend.items())),
        outcomes=outcomes,
        by_result=dict(sorted(by_result.items())),
        by_reason=dict(sorted(by_reason.items())),
        by_attribution=dict(sorted(by_attribution.items())),
        by_origin=dict(sorted(by_origin.items())),
        store_records=store_records,
        store_discarded=store_discarded,
        readings=readings,
        readings_skipped=skipped,
        readings_failed=failed,
        by_gate_reason=dict(sorted(by_gate.items())),
        by_reading_reason=dict(sorted(by_reading.items())),
    )


def _count(counts: collections.Counter[str], value: object) -> None:
    """Count one present closed-vocabulary value.

    An event that omits a field is not counted as a value; a malformed value
    cannot occur at the validated journal boundary, so it is ignored rather
    than named as a category.
    """
    if isinstance(value, str) and value:
        counts[value] += 1


def _integer(value: object) -> int:
    """Read one validated non-negative counter from a journal field.

    Returns
    -------
    int
        The counter, or zero when the field is absent or malformed.
    """
    try:
        return max(0, int(typ.cast("str", value)))
    except TypeError, ValueError:
        return 0


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
