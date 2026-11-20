"""What the writer's active file holds at the moment a run plans.

The archive planner measures the retained store against two bounds, and both
count the rows the writer has not sealed. This value type carries that
snapshot from the scan that read it to the planner and the report: the whole
file's figures, plus the rows and bytes the age rule keeps when the scan was
asked to apply a cutoff. It lives apart from the budget walks so the reader,
the planner and the report can share one shape without depending on the
retention algorithms.
"""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import datetime as dt

    from buildlimits.outcome_segment_inventory import Inventory

__all__ = ["ActiveHistory", "Observed", "observed_history"]


@dc.dataclass(frozen=True, slots=True)
class ActiveHistory:
    """What the writer's active file holds at the moment a run plans.

    Both bounds measure the whole retained store, so the rows the writer has
    not sealed count exactly as closed archives do. This is a snapshot, only
    as current as the scan that produced it.

    Attributes
    ----------
    bytes : int
        Logical bytes in the active file.
    records : int
        Record rows in the active file, excluding the header.
    first_observed_at : datetime | None
        Earliest row observation in the active file, when one was read.
    last_observed_at : datetime | None
        Latest row observation in the active file, when one was read.
    has_metadata : bool
        Whether the rows and span were read. A file that cannot be read at all
        contributes its size alone, and is reported as unmeasured.
    surviving_bytes : int | None
        Logical bytes the age rule keeps when a cutoff was applied to the
        scan, or None when the file was read whole. The writer drops expired
        rows on its next compaction, so only these bytes are retained history.
    surviving_records : int | None
        Record rows observed at or after that cutoff, or None when no cutoff
        was applied.
    """

    bytes: int = 0
    records: int = 0
    first_observed_at: dt.datetime | None = None
    last_observed_at: dt.datetime | None = None
    has_metadata: bool = True
    surviving_bytes: int | None = None
    surviving_records: int | None = None

    @property
    def budget_bytes(self) -> int:
        """The bytes the byte budget counts.

        Returns
        -------
        int
            The rows the age rule keeps when a cutoff was applied, else the
            whole file's size.
        """
        return self.bytes if self.surviving_bytes is None else self.surviving_bytes

    @property
    def budget_records(self) -> int:
        """The rows the record cap counts.

        Returns
        -------
        int
            The rows observed at or after the cutoff when one was applied,
            else every row the file holds.
        """
        if self.surviving_records is None:
            return self.records
        return self.surviving_records


@dc.dataclass(frozen=True, slots=True)
class Observed:
    """What closed metadata and the active file say about the retained rows."""

    records: int
    observed_from: dt.datetime | None
    observed_until: dt.datetime | None
    unmeasured: int


def observed_history(inventory: Inventory, active: ActiveHistory) -> Observed:
    """Summarize every retained row the metadata and the active file describe.

    Returns
    -------
    Observed
        Retained record count, earliest and latest observation, and the number
        of effective entries whose metadata was missing. An entry without
        trustworthy metadata contributes no rows and no span rather than
        guessing, but it is counted so a report never claims completeness.
    """
    described = [
        entry for entry in inventory.effective if entry.record_count is not None
    ]
    records = sum(entry.record_count or 0 for entry in described) + active.records
    unmeasured = sum(1 for entry in inventory.effective if entry.record_count is None)
    unmeasured += 0 if active.has_metadata else 1
    first = [entry.first_observed_at for entry in described if entry.first_observed_at]
    last = [entry.last_observed_at for entry in described if entry.last_observed_at]
    if active.first_observed_at is not None:
        first.append(active.first_observed_at)
    if active.last_observed_at is not None:
        last.append(active.last_observed_at)
    return Observed(
        records=records,
        observed_from=min(first, default=None),
        observed_until=max(last, default=None),
        unmeasured=unmeasured,
    )
