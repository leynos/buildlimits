"""Machine-readable retention limits and retained-history facts.

One report shape answers two different questions an operator must be able to
separate: what policy is in force, and what history that policy currently
retains. Both are derived here, outside the writer and archiver, so the status
document, the archive report and the persisted state cannot describe the same
policy differently.

Retained-store statistics are deliberately *not* cumulative recorder counters:
``record_count`` counts rows still retained on disk, while the recorder's
``attributed`` and ``readings`` counters count outcomes since the process
started. Neither figure is a journal query cap.
"""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_retention import DAY_SECONDS, RetentionPolicy
from buildlimits.utc_format import optional_utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_record_format import StoreRecord

__all__ = [
    "ActiveHistory",
    "RetainedHistory",
    "RetentionLimits",
    "measured_history",
]


@dc.dataclass(frozen=True, slots=True)
class RetentionLimits:
    """The effective bounds one validated policy imposes.

    Attributes
    ----------
    max_age_days : int
        Whole days in the age bound; zero disables age eviction.
    max_age_seconds : int
        The exact age bound in seconds, so a caller never has to convert.
    max_bytes : int
        Logical uncompressed bytes retained; zero disables the byte bound.
    max_records : int | None
        The effective record cap, or None when count is unbounded. A legacy
        store reports its historical 10,000-row cap here rather than None.
    headroom_bytes : int
        Temporary disk space reserved for archive work, outside the budget.
    segment_bytes : int
        The active file high-water mark.
    segmented : bool
        Whether the writer seals closed segments instead of compacting.
    age_eviction : bool
        Whether the age bound is active.
    byte_eviction : bool
        Whether the byte bound is active.
    """

    max_age_days: int
    max_age_seconds: int
    max_bytes: int
    max_records: int | None
    headroom_bytes: int
    segment_bytes: int
    segmented: bool
    age_eviction: bool
    byte_eviction: bool

    @classmethod
    def of(cls, policy: RetentionPolicy) -> typ.Self:
        """Describe the bounds one policy imposes.

        Parameters
        ----------
        policy : RetentionPolicy
            The effective, already-validated policy.

        Returns
        -------
        RetentionLimits
            The JSON-facing description of those bounds.

        Examples
        --------
        >>> RetentionLimits.of(RetentionPolicy()).segmented
        False
        """
        seconds = int(policy.max_age.total_seconds())
        return cls(
            max_age_days=seconds // DAY_SECONDS,
            max_age_seconds=seconds,
            max_bytes=policy.max_bytes,
            max_records=policy.max_records,
            headroom_bytes=policy.headroom_bytes,
            segment_bytes=policy.segment_bytes,
            segmented=policy.is_segmented,
            age_eviction=policy.has_age_limit,
            byte_eviction=policy.has_byte_limit,
        )

    def as_dict(self) -> dict[str, object]:
        """Return the stable JSON fields describing the effective policy.

        Returns
        -------
        dict[str, object]
            A JSON-compatible mapping with no filesystem paths.

        Examples
        --------
        >>> RetentionLimits.of(RetentionPolicy()).as_dict()["max_bytes"]
        12582912
        """
        return {
            "max_age_days": self.max_age_days,
            "max_age_seconds": self.max_age_seconds,
            "max_bytes": self.max_bytes,
            "max_records": self.max_records,
            "headroom_bytes": self.headroom_bytes,
            "segment_bytes": self.segment_bytes,
            "segmented": self.segmented,
            "age_eviction": self.age_eviction,
            "byte_eviction": self.byte_eviction,
        }


@dc.dataclass(frozen=True, slots=True)
class ActiveHistory:
    """What one bounded read of the active file proved about its rows.

    The writer builds this from its in-memory rows; the archiver builds it
    from one bounded read of the active file's complete lines. A file that
    could not be read in full reports ``complete=False`` with a closed reason
    and with counts that are lower bounds rather than guesses.

    Attributes
    ----------
    record_count : int
        Complete rows observed in the active file.
    logical_bytes : int
        The file's logical size, including its header and newlines.
    first_observed_at : datetime | None
        Earliest row observation, when at least one row was read.
    last_observed_at : datetime | None
        Latest row observation, when at least one row was read.
    complete : bool
        Whether every row and its observation time was counted.
    reason : str | None
        A closed reason when the rows could not be counted.
    """

    record_count: int = 0
    logical_bytes: int = 0
    first_observed_at: dt.datetime | None = None
    last_observed_at: dt.datetime | None = None
    complete: bool = True
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the stable JSON fields for one active-file snapshot.

        Returns
        -------
        dict[str, object]
            Counts, the observed span and the completeness marker.
        """
        return {
            "record_count": self.record_count,
            "logical_bytes": self.logical_bytes,
            "first_observed_at": optional_utc_text(self.first_observed_at),
            "last_observed_at": optional_utc_text(self.last_observed_at),
            "complete": self.complete,
            "reason": self.reason,
        }


@dc.dataclass(frozen=True, slots=True)
class RetainedHistory:
    """The history the store currently retains, across every state.

    Attributes
    ----------
    record_count : int
        Rows counted in active, sealed and archived history.
    logical_bytes : int
        Logical uncompressed bytes retained, the figure the budget compares.
    active_bytes : int
        Logical bytes in the writer's active file.
    sealed_bytes : int
        Logical bytes in closed sources awaiting archival.
    archived_logical_bytes : int
        Logical bytes represented by published archives.
    archived_physical_bytes : int
        Disk bytes the published XZ archives occupy, for compression's sake.
    closed_segments : int
        Closed segments, sealed or archived, holding retained history.
    active_records : int
        Rows counted in the active file.
    first_observed_at : datetime | None
        Earliest observation retained, when one is known.
    last_observed_at : datetime | None
        Latest observation retained, when one is known.
    complete : bool
        Whether every retained row and observation time could be counted.
    """

    record_count: int
    logical_bytes: int
    active_bytes: int
    sealed_bytes: int
    archived_logical_bytes: int
    archived_physical_bytes: int
    closed_segments: int
    active_records: int
    first_observed_at: dt.datetime | None
    last_observed_at: dt.datetime | None
    complete: bool

    @property
    def span_seconds(self) -> int | None:
        """The retained observation span, when both ends are known.

        Returns
        -------
        int | None
            Whole seconds between the earliest and latest retained row, or
            None when no row time was counted.
        """
        if self.first_observed_at is None or self.last_observed_at is None:
            return None
        return int((self.last_observed_at - self.first_observed_at).total_seconds())

    def as_dict(self) -> dict[str, object]:
        """Return the stable JSON fields for the retained history.

        Returns
        -------
        dict[str, object]
            Counts, byte accounting, the observation span and the
            completeness marker.
        """
        return {
            "record_count": self.record_count,
            "logical_bytes": self.logical_bytes,
            "active_bytes": self.active_bytes,
            "sealed_bytes": self.sealed_bytes,
            "archived_logical_bytes": self.archived_logical_bytes,
            "archived_physical_bytes": self.archived_physical_bytes,
            "closed_segments": self.closed_segments,
            "active_records": self.active_records,
            "first_observed_at": optional_utc_text(self.first_observed_at),
            "last_observed_at": optional_utc_text(self.last_observed_at),
            "span_seconds": self.span_seconds,
            "complete": self.complete,
        }


def measured_history(
    records: cabc.Iterable[StoreRecord], *, logical_bytes: int
) -> ActiveHistory:
    """Describe rows already held in memory as a complete active snapshot.

    Both the writer, which owns its rows, and the archiver, which has just read
    them, need the same count and observation span. Building the snapshot here
    keeps their diagnostic figures identical.

    Parameters
    ----------
    records : Iterable[StoreRecord]
        The complete active rows, in any order.
    logical_bytes : int
        The active file's logical size, header and newlines included.

    Returns
    -------
    ActiveHistory
        The row count, observation span and completeness marker.

    Examples
    --------
    >>> measured_history([], logical_bytes=12).record_count
    0
    """
    rows = tuple(records)
    if not rows:
        return ActiveHistory(logical_bytes=logical_bytes)
    observed = [record.observed_at for record in rows]
    return ActiveHistory(
        record_count=len(rows),
        logical_bytes=logical_bytes,
        first_observed_at=min(observed),
        last_observed_at=max(observed),
    )
