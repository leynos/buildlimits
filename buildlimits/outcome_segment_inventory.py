"""Read-only inventory of the sealed and archived outcome segments.

Every function here is content-free: it lists regular files and trusts a
validated sidecar rather than decompressing an archive. The writer and the
archiver own mutation, so a scan can never change the store it observes. The
filesystem walk that produces the entries lives in
``buildlimits.outcome_segment_scan``; this module adds the cross-segment view
and the gap and integrity flags a report shows.
"""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_segment_gaps import retained_gaps
from buildlimits.outcome_segment_scan import (
    SegmentEntry,
    regular_files,
    scan_archived,
    scan_sealed,
)
from buildlimits.outcome_segment_sidecar import metadata_fields

if typ.TYPE_CHECKING:
    from buildlimits.outcome_segments import SegmentLayout

__all__ = [
    "Inventory",
    "SegmentEntry",
    "read_inventory",
]


@dc.dataclass(frozen=True, slots=True)
class Inventory:
    """A read-only, sequence-ordered view of closed history.

    Attributes
    ----------
    sealed : tuple[SegmentEntry, ...]
        Sealed but unarchived segments.
    archived : tuple[SegmentEntry, ...]
        Published XZ archives.
    gaps : tuple[int, ...]
        Missing sequences at or above the retention floor, oldest first.
    gaps_truncated : bool
        Whether more sequences are missing than ``gaps`` reports.
    quarantined_files : int
        Regular files the quarantine directory holds. A quarantine name is
        built from the moved file's own name, its fault reason and a random
        token, so it does not reliably carry a sequence; the count is the
        evidence the scan can measure.
    missing_sidecars : tuple[int, ...]
        Archives with no sidecar.
    corrupt_sidecars : tuple[int, ...]
        Archives whose sidecar cannot be trusted.
    orphan_sidecars : tuple[int, ...]
        Sidecars with no archive.
    retention_floor : int | None
        Lowest sequence intentionally retained, when recorded.
    """

    sealed: tuple[SegmentEntry, ...] = ()
    archived: tuple[SegmentEntry, ...] = ()
    gaps: tuple[int, ...] = ()
    gaps_truncated: bool = False
    quarantined_files: int = 0
    missing_sidecars: tuple[int, ...] = ()
    corrupt_sidecars: tuple[int, ...] = ()
    orphan_sidecars: tuple[int, ...] = ()
    retention_floor: int | None = None

    @property
    def effective(self) -> tuple[SegmentEntry, ...]:
        """One entry per sequence, preferring a sealed source.

        A sealed source that still exists beside its archive is the authority:
        publication may have crashed before the source was removed, and reading
        the source avoids trusting an archive that was never reconciled.

        Returns
        -------
        tuple[SegmentEntry, ...]
            Entries in ascending sequence order.
        """
        sealed_sequences = {entry.sequence for entry in self.sealed}
        archived = (
            entry for entry in self.archived if entry.sequence not in sealed_sequences
        )
        merged = [*self.sealed, *archived]
        return tuple(sorted(merged, key=lambda entry: entry.sequence))

    @property
    def highest_sequence(self) -> int | None:
        """The highest closed sequence, if any.

        Returns
        -------
        int | None
            The highest sequence, or None when no closed segment exists.
        """
        sequences = [entry.sequence for entry in self.effective]
        return max(sequences) if sequences else None

    @property
    def sealed_bytes(self) -> int:
        """Uncompressed bytes still awaiting archival.

        Returns
        -------
        int
            Summed sealed file sizes.
        """
        return sum(entry.physical_bytes for entry in self.sealed)

    @property
    def archived_logical_bytes(self) -> int:
        """Logical bytes represented by published archives.

        Returns
        -------
        int
            Summed sidecar logical sizes, falling back to physical size when
            metadata is missing.
        """
        return sum(entry.logical_bytes for entry in self.archived)


def read_inventory(
    layout: SegmentLayout, *, retention_floor: int | None = None
) -> Inventory:
    """Scan closed history without reading segment content.

    Parameters
    ----------
    layout : SegmentLayout
        The layout to scan.
    retention_floor : int | None
        Lowest intentionally retained sequence; sequences below it are not
        reported as gaps.

    Returns
    -------
    Inventory
        Entries in sequence order plus the integrity flags a report shows.

    Notes
    -----
    An unlistable sealed, archive or quarantine directory propagates
    ``OSError`` rather than reporting an empty one: a count that was never
    measured must not read as a measured zero.
    """
    sealed, sealed_meta = scan_sealed(layout.sealed_dir)
    archived, missing, corrupt, orphans = scan_archived(layout.archive_dir)
    with_meta = _with_sealed_metadata(sealed, sealed_meta)
    gaps, gaps_truncated = retained_gaps(with_meta, archived, retention_floor)
    return Inventory(
        sealed=with_meta,
        archived=tuple(sorted(archived, key=lambda entry: entry.sequence)),
        gaps=gaps,
        gaps_truncated=gaps_truncated,
        quarantined_files=len(regular_files(layout.quarantine_dir)),
        missing_sidecars=missing,
        corrupt_sidecars=corrupt,
        orphan_sidecars=orphans,
        retention_floor=retention_floor,
    )


def _with_sealed_metadata(
    sealed: list[SegmentEntry], sealed_meta: dict[int, dict[str, object]]
) -> tuple[SegmentEntry, ...]:
    """Attach each sealed source's writer metadata and order the entries.

    Returns
    -------
    tuple[SegmentEntry, ...]
        The entries in ascending sequence order, each carrying the metadata
        its own sidecar supplied.
    """
    entries = tuple(sorted(sealed, key=lambda entry: entry.sequence))
    return tuple(
        dc.replace(entry, **metadata_fields(sealed_meta[entry.sequence]))
        if entry.sequence in sealed_meta
        else entry
        for entry in entries
    )
