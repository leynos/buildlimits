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
    quarantined : tuple[int, ...]
        Sequences known to be corrupt or unsupported.
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
    quarantined: tuple[int, ...] = ()
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
    """
    sealed, sealed_meta = scan_sealed(layout.sealed_dir)
    archived, missing, corrupt, orphans = scan_archived(layout.archive_dir)
    entries = tuple(sorted(sealed, key=lambda entry: entry.sequence))
    with_meta = tuple(
        dc.replace(entry, **metadata_fields(sealed_meta.get(entry.sequence)))
        if entry.sequence in sealed_meta
        else entry
        for entry in entries
    )
    gaps, gaps_truncated = retained_gaps(with_meta, archived, retention_floor)
    return Inventory(
        sealed=with_meta,
        archived=tuple(sorted(archived, key=lambda entry: entry.sequence)),
        gaps=gaps,
        gaps_truncated=gaps_truncated,
        missing_sidecars=missing,
        corrupt_sidecars=corrupt,
        orphan_sidecars=orphans,
        retention_floor=retention_floor,
    )
