"""Read-only inventory of the sealed and archived outcome segments.

Every function here is content-free: it lists regular files and trusts a
validated sidecar rather than decompressing an archive. The writer and the
archiver own mutation, so a scan can never change the store it observes.
"""

import dataclasses as dc
import os
import re
import stat
import typing as typ

from buildlimits.outcome_segment_sidecar import (
    Sidecar,
    metadata_fields,
    read_mapping,
    read_sidecar,
)
from buildlimits.outcome_segments import SegmentLayout, sidecar_name

if typ.TYPE_CHECKING:
    import datetime as dt
    from pathlib import Path

_ARCHIVE_RE = re.compile(r"^segment-(\d{12})\.jsonl\.xz$")
_SIDECAR_RE = re.compile(r"^segment-(\d{12})\.jsonl\.xz\.json$")
_SEALED_RE = re.compile(r"^segment-(\d{12})\.jsonl$")
_SEALED_META_RE = re.compile(r"^segment-(\d{12})\.jsonl\.meta\.json$")

__all__ = [
    "Inventory",
    "SegmentEntry",
    "read_inventory",
]


@dc.dataclass(frozen=True, slots=True)
class SegmentEntry:
    """One closed segment as the inventory sees it.

    Attributes
    ----------
    sequence : int
        The segment sequence.
    path : Path
        The sealed source or XZ archive.
    state : Literal["sealed", "archived"]
        Which directory owns the entry.
    physical_bytes : int
        Size the file occupies, from ``stat``.
    logical_bytes : int
        Uncompressed bytes represented by the segment.
    record_count : int | None
        Known record count, or None when no metadata describes it.
    first_observed_at : datetime | None
        Earliest row observation, when known.
    last_observed_at : datetime | None
        Latest row observation, when known.
    salt_id : str | None
        Known record domain, when known.
    sha256 : str | None
        Verified content digest, for archives.
    has_metadata : bool
        Whether counts and times came from a trustworthy sidecar.
    """

    sequence: int
    path: Path
    state: typ.Literal["sealed", "archived"]
    physical_bytes: int
    logical_bytes: int
    record_count: int | None = None
    first_observed_at: dt.datetime | None = None
    last_observed_at: dt.datetime | None = None
    salt_id: str | None = None
    sha256: str | None = None
    has_metadata: bool = False


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
        Missing sequences at or above the retention floor.
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
    sealed, sealed_meta = _scan_sealed(layout.sealed_dir)
    archived, missing, corrupt, orphans = _scan_archived(layout.archive_dir)
    entries = tuple(sorted(sealed, key=lambda entry: entry.sequence))
    with_meta = tuple(
        dc.replace(entry, **metadata_fields(sealed_meta.get(entry.sequence)))
        if entry.sequence in sealed_meta
        else entry
        for entry in entries
    )
    sequences = {entry.sequence for entry in with_meta}
    sequences.update(entry.sequence for entry in archived)
    gaps = _gaps(sequences, retention_floor)
    return Inventory(
        sealed=with_meta,
        archived=tuple(sorted(archived, key=lambda entry: entry.sequence)),
        gaps=gaps,
        missing_sidecars=missing,
        corrupt_sidecars=corrupt,
        orphan_sidecars=orphans,
        retention_floor=retention_floor,
    )


def _scan_sealed(
    directory: Path,
) -> tuple[list[SegmentEntry], dict[int, dict[str, object]]]:
    """List sealed sources and their optional writer metadata.

    Returns
    -------
    tuple[list[SegmentEntry], dict[int, dict[str, object]]]
        Sealed entries and metadata keyed by sequence.
    """
    entries: list[SegmentEntry] = []
    metadata: dict[int, dict[str, object]] = {}
    for name, size in _regular_files(directory):
        matched = _SEALED_RE.fullmatch(name)
        if matched is not None:
            sequence = int(matched.group(1))
            entries.append(
                SegmentEntry(
                    sequence=sequence,
                    path=directory / name,
                    state="sealed",
                    physical_bytes=size,
                    logical_bytes=size,
                )
            )
            continue
        meta_match = _SEALED_META_RE.fullmatch(name)
        if meta_match is not None:
            document = read_mapping(directory / name)
            if document is not None:
                metadata[int(meta_match.group(1))] = document
    return entries, metadata


def _scan_archived(
    directory: Path,
) -> tuple[list[SegmentEntry], tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    """List XZ archives, their sidecars and the orphans beside them.

    Returns
    -------
    tuple[list[SegmentEntry], tuple[int, ...], tuple[int, ...], tuple[int, ...]]
        Entries, sequences with missing sidecars, sequences with corrupt
        sidecars, and sequences whose sidecar has no archive.
    """
    entries: list[SegmentEntry] = []
    archive_sequences: set[int] = set()
    sidecar_sequences: set[int] = set()
    missing: list[int] = []
    corrupt: list[int] = []
    for name, size in _regular_files(directory):
        matched = _ARCHIVE_RE.fullmatch(name)
        if matched is not None:
            sequence = int(matched.group(1))
            archive_sequences.add(sequence)
            sidecar = read_sidecar(directory / sidecar_name(sequence), sequence)
            if sidecar is None:
                missing.append(sequence)
                entries.append(
                    SegmentEntry(
                        sequence=sequence,
                        path=directory / name,
                        state="archived",
                        physical_bytes=size,
                        logical_bytes=size,
                    )
                )
            elif isinstance(sidecar, Sidecar):
                sidecar_sequences.add(sequence)
                entries.append(
                    SegmentEntry(
                        sequence=sequence,
                        path=directory / name,
                        state="archived",
                        physical_bytes=size,
                        logical_bytes=sidecar.logical_bytes,
                        record_count=sidecar.record_count,
                        first_observed_at=sidecar.first_observed_at,
                        last_observed_at=sidecar.last_observed_at,
                        salt_id=sidecar.salt_id,
                        sha256=sidecar.sha256,
                        has_metadata=True,
                    )
                )
            else:
                corrupt.append(sequence)
                entries.append(
                    SegmentEntry(
                        sequence=sequence,
                        path=directory / name,
                        state="archived",
                        physical_bytes=size,
                        logical_bytes=size,
                    )
                )
            continue
        side_match = _SIDECAR_RE.fullmatch(name)
        if side_match is not None:
            sidecar_sequences.add(int(side_match.group(1)))
    orphans = tuple(sorted(sidecar_sequences - archive_sequences))
    return entries, tuple(sorted(missing)), tuple(sorted(corrupt)), orphans


def _regular_files(directory: Path) -> list[tuple[str, int]]:
    """List regular files in one directory without following links.

    Returns
    -------
    list[tuple[str, int]]
        Name and size of every regular file, or an empty list when the
        directory does not exist.
    """
    try:
        iterator = os.scandir(directory)
    except OSError:
        return []
    found: list[tuple[str, int]] = []
    try:
        for entry in iterator:
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(metadata.st_mode):
                found.append((entry.name, metadata.st_size))
    finally:
        iterator.close()
    return found


def _gaps(sequences: set[int], retention_floor: int | None) -> tuple[int, ...]:
    """Return missing sequences at or above the retention floor.

    Returns
    -------
    tuple[int, ...]
        Missing sequences in ascending order.

    Examples
    --------
    >>> _gaps({2, 4}, 2)
    (3,)
    >>> _gaps({3, 4}, 3)
    ()
    """
    if not sequences:
        return ()
    lowest = 1 if retention_floor is None else max(1, retention_floor)
    highest = max(sequences)
    return tuple(
        sequence for sequence in range(lowest, highest + 1) if sequence not in sequences
    )
