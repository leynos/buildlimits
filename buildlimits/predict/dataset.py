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
from buildlimits.predict.records import build_events
from buildlimits.utc_text import utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_record_format import DecodedStore
    from buildlimits.predict.events import Event

SNAPSHOT_VERSION = 1
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

    def to_dict(self) -> dict[str, object]:
        """Return the snapshot shape for one gap.

        Returns
        -------
        dict[str, object]
            The gap under stable keys.
        """
        return {
            "start": utc_text(self.start),
            "end": utc_text(self.end),
            "seconds": round(self.seconds, 3),
        }


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

    def to_dict(self) -> dict[str, object]:
        """Return the snapshot shape for the store provenance.

        Returns
        -------
        dict[str, object]
            The provenance under stable keys.
        """
        return {
            "kind": "cache-outcomes",
            "salt_id": self.salt_id,
            "written_at": utc_text(self.written_at),
            "records_total": self.records_total,
            "outcome_records": self.outcome_records,
            "summary_records": self.summary_records,
        }


@dc.dataclass(frozen=True, slots=True)
class Snapshot:
    """A redacted, chronologically ordered event stream plus its provenance.

    Attributes
    ----------
    created_at : datetime
        When the snapshot was written.
    source : Source
        The store provenance read at the same moment.
    journal : dict[str, object]
        What the user journal could offer, or why it offered nothing.
    events : tuple[Event, ...]
        Every recorded compilation, oldest label first.
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
            Counts, labelled share, time span and identities seen.
        """
        labelled = sum(1 for event in self.events if event.label != "unknown")
        total = len(self.events)
        return {
            "events": total,
            "labelled": labelled,
            "labelled_fraction": 0.0 if total == 0 else labelled / total,
            "identities": len({event.identity for event in self.events}),
            "span_start": utc_text(self.events[0].observed_at) if self.events else None,
            "span_end": utc_text(self.events[-1].observed_at) if self.events else None,
            "gaps": [gap.to_dict() for gap in self.gaps],
        }

    def to_dict(self) -> dict[str, object]:
        """Return the complete snapshot document.

        Returns
        -------
        dict[str, object]
            The versioned document written to disk.
        """
        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "created_at": utc_text(self.created_at),
            "source": self.source.to_dict(),
            "journal": self.journal,
            "stats": self.stats(),
            "events": [event.to_dict() for event in self.events],
        }


def find_gaps(
    events: cabc.Sequence[Event], *, threshold_seconds: float
) -> tuple[Gap, ...]:
    """Report silences longer than the configured threshold.

    Parameters
    ----------
    events : Sequence[Event]
        The history in chronological order; gaps are measured between
        neighbours.
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

    Adapter records carry no compilation identity, so they cannot be joined to
    store rows. The counts are reported as context, not used as events.

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
        Availability, counts by source and event, and the join limitation.
    """
    try:
        window = query(since=since, max_records=_MAX_JOURNAL_RECORDS, source="all")
    except metrics_query.MetricsQueryError as error:
        return {"available": False, "span": since, "reason": str(error)}
    by_source: dict[str, int] = {}
    by_event: dict[str, int] = {}
    for record in window.records:
        by_source[record.source] = by_source.get(record.source, 0) + 1
        by_event[record.event] = by_event.get(record.event, 0) + 1
    return {
        "available": bool(window.records),
        "span": since,
        "truncated": window.truncated,
        "records": len(window.records),
        "by_source": dict(sorted(by_source.items())),
        "by_event": dict(sorted(by_event.items())),
        "join": "adapter records carry no compilation identity, so journal "
        "counts are reported as context and are not joined to store rows",
    }


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
