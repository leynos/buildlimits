"""Build and redact the versioned cache-outcome history snapshot.

The live store rotates and the journal is capped, so the benchmark never reads
either directly. :func:`build_snapshot` turns the store's attributed records
into a stable event stream with the provenance needed to judge how much history
it really represents; :mod:`buildlimits.predict.snapshot_store` writes and
reloads that document.
"""

import dataclasses as dc
import itertools
import typing as typ

from buildlimits import metrics_query
from buildlimits.metrics_query import MAX_RECORDS as _MAX_JOURNAL_RECORDS
from buildlimits.outcome_record_format import OutcomeRecord
from buildlimits.predict.events import admission_moment
from buildlimits.predict.records import build_events
from buildlimits.predict.snapshot_codec import gap_to_dict
from buildlimits.utc_text import utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_record_format import DecodedStore
    from buildlimits.predict.events import Event

DEFAULT_JOURNAL_SPAN = "4w"


@dc.dataclass(frozen=True, slots=True)
class Gap:
    """A silence in the history long enough to hide lost records.

    Attributes
    ----------
    start : datetime
        Time of the last record before the silence.
    end : datetime
        Time of the first record after the silence.
    seconds : float
        Length of the silence.
    """

    start: dt.datetime
    end: dt.datetime
    seconds: float


@dc.dataclass(frozen=True, slots=True)
class Source:
    """What the store itself said when the snapshot was taken.

    Attributes
    ----------
    salt_id : str
        The store's non-secret key identifier.
    written_at : datetime
        The store header's write time.
    records_total : int
        Every row read, summaries included.
    outcome_records : int
        Attributed request rows.
    summary_records : int
        Window-summary rows, which carry no per-compilation identity.
    """

    salt_id: str
    written_at: dt.datetime
    records_total: int
    outcome_records: int
    summary_records: int


@dc.dataclass(frozen=True, slots=True)
class Snapshot:
    """A redacted event stream in store order, plus its provenance.

    Attributes
    ----------
    created_at : datetime
        When the snapshot was written.
    source : Source
        The store provenance read at the same moment.
    journal : dict[str, object]
        What the user journal could offer, or why it offered nothing.
    events : tuple[Event, ...]
        Every recorded compilation, in the order the store appended it.
    gaps : tuple[Gap, ...]
        Silences longer than the configured threshold.
    """

    created_at: dt.datetime
    source: Source
    journal: dict[str, object]
    events: tuple[Event, ...]
    gaps: tuple[Gap, ...]

    def stats(self) -> dict[str, object]:
        """Summarize the history the snapshot really holds.

        Returns
        -------
        dict[str, object]
            Counts, labelled share, time span, identities seen and how many
            events carry an admission time a prediction could use.
        """
        labelled = sum(1 for event in self.events if event.label != "unknown")
        total = len(self.events)
        observed = [event.observed_at for event in self.events]
        recorded = [event for event in self.events if event.admitted_at is not None]
        usable = sum(1 for event in self.events if admission_moment(event) is not None)
        return {
            "events": total,
            "labelled": labelled,
            "labelled_fraction": 0.0 if total == 0 else labelled / total,
            "identities": len({event.identity for event in self.events}),
            "span_start": utc_text(min(observed)) if observed else None,
            "span_end": utc_text(max(observed)) if observed else None,
            "admission_recorded": len(recorded),
            "admission_usable": usable,
            "admission_missing": total - len(recorded),
            "admission_unusable": len(recorded) - usable,
            "gaps": [gap_to_dict(gap) for gap in self.gaps],
        }


def find_gaps(
    events: cabc.Sequence[Event], *, threshold_seconds: float
) -> tuple[Gap, ...]:
    """Report silences longer than the configured threshold.

    Parameters
    ----------
    events : Sequence[Event]
        The history in store append order; gaps are measured between
        neighbours, and a backward clock step shows a negative silence rather
        than a gap.
    threshold_seconds : float
        The shortest silence, in seconds, that counts as a gap.

    Returns
    -------
    tuple[Gap, ...]
        The gaps, oldest first.
    """
    gaps: list[Gap] = []
    for earlier, later in itertools.pairwise(events):
        seconds = (later.observed_at - earlier.observed_at).total_seconds()
        if seconds > threshold_seconds:
            gaps.append(Gap(earlier.observed_at, later.observed_at, seconds))
    return tuple(gaps)


def count_journal(
    since: str = DEFAULT_JOURNAL_SPAN,
    *,
    query: cabc.Callable[..., metrics_query.Window] = metrics_query.query,
) -> dict[str, object]:
    """Describe what the user journal currently offers the benchmark.

    The journal is the only source that could bound retention, but neither
    source it names carries a joinable compilation key: adapter records hold
    per-compilation metrics without an identity, and supervisor records hold
    admission and queue events without a compilation identity. The join is
    therefore reported as unavailable, with the record counts and the
    retention span that *are* available, rather than guessed at.

    The result is deliberately an ordinary mapping rather than a typed record.
    It is the provenance block the snapshot stores and the report renders, and
    an unavailable journal becomes a shorter mapping that names its reason at
    the command boundary: every key below is optional by contract, and the
    renderer reports only what is present. A record here would have to be
    flattened back to this mapping at the same boundary, so it would add a
    second definition of the block without constraining either consumer.

    Parameters
    ----------
    since : str
        The journal span to ask for, such as ``"24h"``.
    query : Callable[..., Window]
        The journal reader, injectable so a test or an offline run can answer
        without the journal.

    Returns
    -------
    dict[str, object]
        Availability, counts by source and event, the supervisor starts the
        window saw, the retention the query covers and the correlation it
        cannot support.

    A journal that cannot be read raises ``metrics_query.MetricsQueryError``.
    Deciding that provenance may continue without it belongs to the command
    boundary, which knows the snapshot is written from the store and must not
    be lost to a side query.
    """
    window = query(since=since, max_records=_MAX_JOURNAL_RECORDS, source="all")
    by_source: dict[str, int] = {}
    by_event: dict[str, int] = {}
    for record in window.records:
        by_source[record.source] = by_source.get(record.source, 0) + 1
        by_event[record.event] = by_event.get(record.event, 0) + 1
    stamps = sorted(
        record.timestamp for record in window.records if record.timestamp is not None
    )
    starts = [
        record.timestamp
        for record in window.records
        if record.event == "supervisor_started"
    ]
    dated_starts = sorted(stamp for stamp in starts if stamp is not None)
    return {
        "available": bool(window.records),
        "span": since,
        "truncated": window.truncated,
        "records": len(window.records),
        "span_start": utc_text(stamps[0]) if stamps else None,
        "span_end": utc_text(stamps[-1]) if stamps else None,
        "by_source": dict(sorted(by_source.items())),
        "by_event": dict(sorted(by_event.items())),
        "restarts": {
            "count": len(starts),
            "first": utc_text(dated_starts[0]) if dated_starts else None,
            "last": utc_text(dated_starts[-1]) if dated_starts else None,
        },
        "retention": _retention_note(since, truncated=window.truncated),
        "correlation": _CORRELATION_LIMIT,
    }


_CORRELATION_LIMIT = (
    "no joinable per-compilation admission record: adapter rows carry metrics "
    "without an identity and supervisor rows carry queue events without a "
    "compilation identity, so the journal bounds retention and adds no "
    "admission time to a store row"
)


def _retention_note(since: str, *, truncated: bool) -> str:
    """Describe how far back the journal query is reliable.

    Returns
    -------
    str
        The requested lookback, the record cap and whether the cap was hit.
    """
    note = (
        f"at most {_MAX_JOURNAL_RECORDS} records over the requested {since} "
        "lookback; older records are outside what this query can see"
    )
    if truncated:
        note += ", and the cap was reached so the newest records won"
    return note


def build_snapshot(
    decoded: DecodedStore,
    *,
    now: dt.datetime,
    journal: dict[str, object] | None = None,
    gap_threshold_seconds: float = 3600.0,
) -> Snapshot:
    """Assemble the snapshot document from a decoded store.

    Parameters
    ----------
    decoded : DecodedStore
        The store header and every record read from it.
    now : datetime
        The creation time recorded in the snapshot, supplied so a run is
        reproducible.
    journal : dict[str, object] | None
        The journal counts to keep as provenance, or None to query nothing.
    gap_threshold_seconds : float
        The shortest silence that the snapshot reports as a gap.

    Returns
    -------
    Snapshot
        The redacted history with provenance, counts and gaps.
    """
    records = decoded.records
    outcomes = sum(1 for record in records if isinstance(record, OutcomeRecord))
    events = build_events(records)
    source = Source(
        salt_id=decoded.header.salt_id,
        written_at=decoded.header.written_at,
        records_total=len(records),
        outcome_records=outcomes,
        summary_records=len(records) - outcomes,
    )
    return Snapshot(
        created_at=now,
        source=source,
        journal={} if journal is None else journal,
        events=events,
        gaps=find_gaps(events, threshold_seconds=gap_threshold_seconds),
    )
