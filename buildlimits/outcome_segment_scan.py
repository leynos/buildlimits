"""List the sealed and archived segment files without reading their content.

A scan is the only part of the inventory that touches the filesystem, so it
lives apart from the value types it reports. Every function here is
content-free: it lists regular files and trusts a validated sidecar rather
than decompressing an archive.
"""

import dataclasses as dc
import os
import re
import stat
import typing as typ

from buildlimits.outcome_archive_dirs import open_private_chain_when_present
from buildlimits.outcome_segment_sidecar import (
    Sidecar,
    read_mapping,
    read_sidecar,
)
from buildlimits.outcome_segments import sidecar_name

if typ.TYPE_CHECKING:
    import datetime as dt
    from pathlib import Path

_ARCHIVE_RE = re.compile(r"^segment-(\d{12})\.jsonl\.xz$")
_SIDECAR_RE = re.compile(r"^segment-(\d{12})\.jsonl\.xz\.json$")
_SEALED_RE = re.compile(r"^segment-(\d{12})\.jsonl$")
_SEALED_META_RE = re.compile(r"^segment-(\d{12})\.jsonl\.meta\.json$")
_REPLACING_RE = re.compile(r"^segment-(\d{12})\.jsonl\.xz\.replacing$")

__all__ = [
    "SegmentEntry",
    "regular_files",
    "scan_archived",
    "scan_sealed",
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


def scan_sealed(
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
    for path, size in regular_files(directory):
        matched = _SEALED_RE.fullmatch(path.name)
        if matched is not None:
            sequence = int(matched.group(1))
            entries.append(
                SegmentEntry(
                    sequence=sequence,
                    path=path,
                    state="sealed",
                    physical_bytes=size,
                    logical_bytes=size,
                )
            )
            continue
        meta_match = _SEALED_META_RE.fullmatch(path.name)
        if meta_match is not None:
            document = read_mapping(path)
            if document is not None:
                metadata[int(meta_match.group(1))] = document
    return entries, metadata


def scan_archived(
    directory: Path,
) -> tuple[list[SegmentEntry], tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    """List XZ archives, their sidecars and the orphans beside them.

    Returns
    -------
    tuple[list[SegmentEntry], tuple[int, ...], tuple[int, ...], tuple[int, ...]]
        Entries, sequences with missing sidecars, sequences with corrupt
        sidecars, and sequences whose sidecar has no archive.
    """
    files = regular_files(directory)
    archives = {
        int(archive.group(1)): (path, size)
        for path, size in files
        if (archive := _ARCHIVE_RE.fullmatch(path.name)) is not None
    }
    named_sidecars = _sidecar_sequences(files)
    entries, missing, corrupt = _classify_archives(
        directory, archives, _replacing_sequences(files)
    )
    orphans = tuple(sorted(named_sidecars - archives.keys()))
    return entries, missing, corrupt, orphans


def _sequence_set(files: list[tuple[Path, int]], pattern: re.Pattern[str]) -> set[int]:
    """Return every sequence a file in this listing names for ``pattern``.

    The sidecar and replacing-marker conventions differ only in their infix,
    so the walk is shared and each caller names the convention it reads.

    Returns
    -------
    set[int]
        Sequences named by a matching file, whether or not its archive exists.
    """
    return {
        int(match.group(1))
        for path, _size in files
        if (match := pattern.fullmatch(path.name)) is not None
    }


def _sidecar_sequences(files: list[tuple[Path, int]]) -> set[int]:
    """Return every sequence a sidecar file in this listing names.

    Returns
    -------
    set[int]
        Sequences with a sidecar entry, whether or not its archive exists.
    """
    return _sequence_set(files, _SIDECAR_RE)


def _replacing_sequences(files: list[tuple[Path, int]]) -> set[int]:
    """Return every sequence whose rewrite has not finished.

    A marker is read from the whole listing before any sidecar is, because a
    scan that trusted the sidecar the rewrite has already made stale would
    report the previous archive's counts beside the new bytes.

    Returns
    -------
    set[int]
        Sequences a rewrite is still replacing.
    """
    return _sequence_set(files, _REPLACING_RE)


def _classify_archives(
    directory: Path,
    archives: dict[int, tuple[Path, int]],
    replacing: set[int],
) -> tuple[list[SegmentEntry], tuple[int, ...], tuple[int, ...]]:
    """Describe each archive and separate its trustworthy sidecars.

    Returns
    -------
    tuple[list[SegmentEntry], tuple[int, ...], tuple[int, ...]]
        Entries in sequence order, sequences with a missing sidecar, and
        sequences whose sidecar cannot be trusted.
    """
    classified = [
        (
            sequence,
            _classify_archive(
                directory, sequence, (path, size), is_replacing=sequence in replacing
            ),
        )
        for sequence, (path, size) in sorted(archives.items())
    ]
    entries = [entry for _sequence, (entry, _verdict) in classified]
    missing = _verdict_sequences(classified, "missing")
    corrupt = _verdict_sequences(classified, "corrupt")
    return entries, missing, corrupt


def _verdict_sequences(
    classified: list[
        tuple[int, tuple[SegmentEntry, typ.Literal["trusted", "missing", "corrupt"]]]
    ],
    verdict: typ.Literal["missing", "corrupt"],
) -> tuple[int, ...]:
    """Return the sequences whose sidecar scan reached ``verdict``.

    Returns
    -------
    tuple[int, ...]
        The matching sequences in the order the classification produced them.
    """
    return tuple(
        sequence for sequence, (_entry, found) in classified if found == verdict
    )


def _classify_archive(
    directory: Path,
    sequence: int,
    archive: tuple[Path, int],
    *,
    is_replacing: bool,
) -> tuple[SegmentEntry, typ.Literal["trusted", "missing", "corrupt"]]:
    """Describe one archive and decide whether its sidecar may be believed.

    Returns
    -------
    tuple[SegmentEntry, Literal["trusted", "missing", "corrupt"]]
        The entry and whether its sidecar was trusted, absent or refused.
    """
    path, size = archive
    if is_replacing:
        # A rewrite that has not recorded its new sidecar may already have
        # published bytes the old sidecar cannot describe. Only the archive
        # itself can say what it holds.
        return _archive_entry(path, size, sequence), "corrupt"
    match read_sidecar(directory / sidecar_name(sequence), sequence):
        case None:
            return _archive_entry(path, size, sequence), "missing"
        case Sidecar() as sidecar if sidecar.physical_bytes == size:
            return _archive_entry(path, size, sequence, sidecar=sidecar), "trusted"
        case _:
            # A sidecar written for different bytes than the archive now holds
            # describes a rewrite that stopped before its metadata was
            # replaced. Trusting it would report the old counts beside the new
            # file, so the archive is routed to the repair path.
            return _archive_entry(path, size, sequence), "corrupt"


def _archive_entry(
    path: Path,
    size: int,
    sequence: int,
    *,
    sidecar: Sidecar | None = None,
) -> SegmentEntry:
    """Describe one archive, trusting its sidecar only when one was validated.

    Without a trustworthy sidecar the archive's logical size falls back to its
    physical size. That figure does not describe the history the archive
    holds. The container header can make it larger than the content, so it
    overstates as well as understates, and a run that heals sidecars plans
    retention again from the repaired inventory before it evicts anything.

    Parameters
    ----------
    path : Path
        The archive file.
    size : int
        The archive's physical size in bytes.
    sequence : int
        The sequence parsed from the file name.
    sidecar : Sidecar | None
        The validated sidecar, when one described this archive.

    Returns
    -------
    SegmentEntry
        The archived entry.
    """
    if sidecar is None:
        return SegmentEntry(
            sequence=sequence,
            path=path,
            state="archived",
            physical_bytes=size,
            logical_bytes=size,
        )
    return SegmentEntry(
        sequence=sequence,
        path=path,
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


def regular_files(directory: Path) -> list[tuple[Path, int]]:
    """List regular files in one directory without following links.

    The directory's whole chain is validated before it is listed, and the
    listing then reads through that same open descriptor. ``scandir`` follows a
    symlinked directory argument, and a path re-opened after validation could
    be redirected by replacing a component; either would name files outside
    the store that a retention pass then deletes. A missing directory is an
    empty listing, but any other listing or validation failure propagates:
    reporting no entries for an unreadable directory would present damage as
    an empty store.

    Returns
    -------
    list[tuple[Path, int]]
        Path and size of every regular file, or an empty list when the
        directory does not exist.

    Notes
    -----
    A refused chain propagates `outcome_archive_dirs.UnsafePathError`.
    """
    descriptor = open_private_chain_when_present(directory)
    if descriptor is None:
        return []
    found: list[tuple[Path, int]] = []
    try:
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    # The entry was removed between listing and stat; it is
                    # gone rather than unreadable, so it is not in the scan.
                    continue
                if stat.S_ISREG(metadata.st_mode):
                    found.append((directory / entry.name, metadata.st_size))
    finally:
        os.close(descriptor)
    return found
