"""Build and redact the versioned cache-outcome history snapshot.

The live store rotates and the journal is capped, so the benchmark never reads
either directly. :func:`build_snapshot` turns an already-redacted event stream
into a stable document with the provenance needed to judge how much history it
really represents. Decoding the store into that stream belongs to
:mod:`buildlimits.predict.records`, and
:mod:`buildlimits.predict.snapshot_store` writes and reloads the document, so
this module holds values and counting rather than readers.
"""

import dataclasses as dc
import itertools
import typing as typ

from buildlimits.predict.accounting import Diagnostics, HistoryLoss
from buildlimits.predict.accounting import StoreAccounting as StoreAccounting
from buildlimits.predict.events import (
    admission_moment,
    label_moment,
    unverified_label,
)
from buildlimits.predict.timing import audit as timing_audit
from buildlimits.predict.timing import exclusions as timing_exclusions
from buildlimits.predict.timing import scored as timing_scored

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.predict.events import Event
    from buildlimits.predict.journal_provenance import JournalProvenance


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
        The newest store header's non-secret key identifier.
    salt_ids : tuple[str, ...]
        Every domain the snapshot spans, oldest first. The counts below cover
        all of them, while ``salt_id`` names only the newest; an empty tuple
        means the snapshot came from one domain and ``salt_id`` is the whole
        story.
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
    salt_ids: tuple[str, ...] = ()


@dc.dataclass(frozen=True, slots=True)
class StoreHistory:
    """The event stream a decoded store holds, with its store provenance.

    Attributes
    ----------
    events : tuple[Event, ...]
        Every attributed record, in the order the store appended it.
    source : Source
        The header and counts the events were read from.
    accounting : StoreAccounting
        Bounded counts explaining how those rows did or did not become labels.
    """

    events: tuple[Event, ...]
    source: Source
    accounting: StoreAccounting


@dc.dataclass(frozen=True, slots=True)
class SnapshotStats:
    """What the snapshot's event stream really represents.

        The timing counts separate the two independent gates a scorable record
        must pass: a trustworthy decision time and a trustworthy label-availability
        time. ``scored`` is the intersection, and ``excluded`` states why the rest
        are not scored, so more labels alone never read as chronological validity.

    Attributes
    ----------
        events : int
            Every event in the history.
        labelled : int
            Events carrying a hit or miss label.
        labelled_fraction : float
            The labelled share of the history, or zero for an empty one.
        identities : int
            Distinct compilation identities seen.
        span_start : datetime | None
            The earliest observation, or None for an empty history.
        span_end : datetime | None
            The latest observation, or None for an empty history.
        admission_recorded : int
            Events carrying an admission time.
        admission_usable : int
            Events whose admission time is at or before their verified
            availability time, or before their historical label time where
            availability was never verified.
        admission_missing : int
            Events carrying no admission time at all.
        admission_unusable : int
    Events whose recorded admission time is after their label.
        unverified_label_time : int
            Hit or miss labels whose recorded observation time is unverified, so
            the replay withholds them rather than scoring an unsound release time.
        availability_recorded : int
            Events whose label time is proven.
        availability_usable : int
            Proven events whose decision is at or before that time, so a
            prediction could have released the label.
        availability_missing : int
            Events with no proven label time at all.
        scored : int
            Events that pass every timing, label and correlation gate.
        excluded : Mapping[str, int]
            Scored-population exclusions by closed reason.
        duplicates : int
            Rows that repeated an attempt with identical evidence.
        conflicts : int
            Rows whose attempt identity also appeared with different evidence.
        gaps : tuple[Gap, ...]
            Silences longer than the configured threshold.
    """

    events: int
    labelled: int
    labelled_fraction: float
    identities: int
    span_start: dt.datetime | None
    span_end: dt.datetime | None
    admission_recorded: int
    admission_usable: int
    admission_missing: int
    admission_unusable: int
    unverified_label_time: int
    availability_recorded: int
    availability_usable: int
    availability_missing: int
    scored: int
    excluded: cabc.Mapping[str, int]
    duplicates: int
    conflicts: int
    gaps: tuple[Gap, ...]


@dc.dataclass(frozen=True, slots=True)
class Snapshot:
    """A redacted event stream in store order, plus its provenance.

    Attributes
    ----------
    created_at : datetime
        When the snapshot was written.
    source : Source
        The store provenance read at the same moment.
    journal : JournalProvenance | None
        What the user journal could offer, why it offered nothing, or None
        when the snapshot queried no journal.
    events : tuple[Event, ...]
        Every recorded compilation, in the order the store appended it.
    gaps : tuple[Gap, ...]
        Silences longer than the configured threshold.
    diagnostics : Diagnostics | None
        The retained-store and history-loss counts, or None for a snapshot
        written before those counts existed.
    """

    created_at: dt.datetime
    source: Source
    journal: JournalProvenance | None
    events: tuple[Event, ...]
    gaps: tuple[Gap, ...]
    diagnostics: Diagnostics | None = None

    def stats(self) -> SnapshotStats:
        """Summarize the history the snapshot really holds.

        Returns
        -------
        SnapshotStats
            Counts, labelled share, time span, identities seen, and the
            decision, availability and scored populations with their closed
            exclusion reasons.
        """
        labelled = sum(1 for event in self.events if event.label != "unknown")
        total = len(self.events)
        observed = [event.observed_at for event in self.events]
        recorded = [event for event in self.events if event.admitted_at is not None]
        usable = sum(1 for event in self.events if admission_moment(event) is not None)
        unverified = sum(
            1 for event in self.events if unverified_label(event) is not None
        )
        verdict = timing_audit(self.events)
        availability = _availability_counts(self.events)
        return SnapshotStats(
            events=total,
            labelled=labelled,
            labelled_fraction=0.0 if total == 0 else labelled / total,
            identities=len({event.identity for event in self.events}),
            span_start=min(observed) if observed else None,
            span_end=max(observed) if observed else None,
            admission_recorded=len(recorded),
            admission_usable=usable,
            admission_missing=total - len(recorded),
            admission_unusable=len(recorded) - usable,
            unverified_label_time=unverified,
            availability_recorded=availability.recorded,
            availability_usable=availability.usable,
            availability_missing=total - availability.recorded,
            scored=timing_scored(verdict),
            excluded=timing_exclusions(verdict),
            duplicates=verdict.duplicates,
            conflicts=verdict.conflicts,
            gaps=self.gaps,
        )


@dc.dataclass(frozen=True, slots=True)
class _AvailabilityCounts:
    """How much of one history proves when its labels became available."""

    recorded: int
    usable: int


def _availability_counts(events: cabc.Sequence[Event]) -> _AvailabilityCounts:
    """Count the events whose label time is proven, and how many are usable.

    Returns
    -------
    _AvailabilityCounts
        The recorded and usable availability populations.
    """
    proven = [event for event in events if label_moment(event) is not None]
    usable = sum(
        1
        for event in proven
        if event.admitted_at is not None
        and event.admitted_at <= typ.cast("dt.datetime", label_moment(event))
    )
    return _AvailabilityCounts(recorded=len(proven), usable=usable)


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


def build_snapshot(  # ruff: ignore[too-many-arguments] - one explicit input per provenance source; pylint: disable=too-many-arguments
    history: StoreHistory,
    *,
    now: dt.datetime,
    history_loss: HistoryLoss,
    journal: JournalProvenance | None = None,
    gap_threshold_seconds: float = 3600.0,
) -> Snapshot:
    """Assemble the snapshot document from a decoded history.

    Parameters
    ----------
    history : StoreHistory
        The redacted events and the store provenance they came from.
    now : datetime
        The creation time recorded in the snapshot, supplied so a run is
        reproducible.
    journal : JournalProvenance | None
        The journal provenance to keep, or None when no journal was queried.
    gap_threshold_seconds : float
        The shortest silence that the snapshot reports as a gap.
    history_loss : HistoryLoss
        What the reader established about closed history. It is required
        because the snapshot must state the scope it really read; a default
        would let a caller claim the active file alone was read without having
        decided that.

    Returns
    -------
    Snapshot
        The redacted history with provenance, counts, gaps and diagnostics.
    """
    return Snapshot(
        created_at=now,
        source=history.source,
        journal=journal,
        events=history.events,
        gaps=find_gaps(history.events, threshold_seconds=gap_threshold_seconds),
        diagnostics=Diagnostics(store=history.accounting, history=history_loss),
    )
