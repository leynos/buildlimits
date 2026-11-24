"""One bounded snapshot of the writer's active file.

The planner, the writer's status document and the archive report all describe
the same active file, so they share one value shape: the whole-file figures
beside the bytes and rows the writer keeps. The type carries no I/O, so a
domain module can name it without depending on the store.
"""

import dataclasses as dc
import typing as typ

from buildlimits.utc_format import optional_utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_record_format import StoreRecord

__all__ = ["ActiveHistory", "measured_history"]


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
        Logical bytes the writer's next compaction keeps: the header plus
        every committed row observed at or after the cutoff, or every
        committed row when none was applied. None when the file could not be
        read, because then neither figure is known. The writer drops expired
        rows and rolls back an unterminated append, so only these bytes are
        retained history.
    surviving_records : int | None
        Record rows observed at or after that cutoff, or every committed row
        when no cutoff was applied; None when the file could not be read.
    reason : str | None
        A closed reason the rows and span are unknown. It is set only when
        ``has_metadata`` is false, so a status reader can name the fault
        without parsing the store itself.
    """

    bytes: int = 0
    records: int = 0
    first_observed_at: dt.datetime | None = None
    last_observed_at: dt.datetime | None = None
    has_metadata: bool = True
    surviving_bytes: int | None = None
    surviving_records: int | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the stable JSON fields for one active-file snapshot.

        The names are the ones the status document's ``retention`` object
        publishes, so the rendered line and the machine-readable document
        describe the same measurement.

        Returns
        -------
        dict[str, object]
            Counts, the observed span, the completeness marker and the closed
            reason when the rows could not be read.

        Examples
        --------
        >>> ActiveHistory(bytes=12).as_dict()["complete"]
        True
        """
        return {
            "record_count": self.records,
            "logical_bytes": self.bytes,
            "first_observed_at": optional_utc_text(self.first_observed_at),
            "last_observed_at": optional_utc_text(self.last_observed_at),
            "complete": self.has_metadata,
            "reason": self.reason,
        }

    @property
    def budget_bytes(self) -> int:
        """The bytes the byte budget counts.

        The whole-file fallback is only ever a report estimate. It includes an
        append the writer may roll back, so a planner with a byte bound defers
        that bound until a later scan reads the committed bytes.

        Returns
        -------
        int
            The header and committed rows the writer keeps, or the whole
            file's size when it could not be read and neither figure is known.
        """
        return self.bytes if self.surviving_bytes is None else self.surviving_bytes

    @property
    def budget_records(self) -> int:
        """The rows the record cap counts.

        Returns
        -------
        int
            The committed rows the writer keeps, or zero when the file could
            not be read. A cap configured beside an unread file is refused
            before a plan is built, so the zero is never taken as a count.
        """
        if self.surviving_records is None:
            return self.records
        return self.surviving_records


def measured_history(
    records: cabc.Iterable[StoreRecord], *, logical_bytes: int
) -> ActiveHistory:
    """Describe rows already held in memory as a complete active snapshot.

    The writer owns its rows and can describe them without reading the file
    again, while the archiver reads the same figures through a bounded scan.
    Building both here keeps their diagnostic values identical.

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
    >>> measured_history([], logical_bytes=12).records
    0
    """
    rows = tuple(records)
    if not rows:
        return ActiveHistory(bytes=logical_bytes)
    observed = [record.observed_at for record in rows]
    return ActiveHistory(
        bytes=logical_bytes,
        records=len(rows),
        first_observed_at=min(observed),
        last_observed_at=max(observed),
    )
