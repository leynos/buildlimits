"""Per-segment file operations for the cache-outcome archiver.

Every operation here runs under the archive lock, never touches the active
file, and publishes only a verified temporary file. A sealed source is removed
after its archive, sidecar and directory are durable; an archive is replaced
only by an atomic rename of a verified temporary file. Failures name a closed
reason and leave the previous state readable.
"""

import contextlib
import dataclasses as dc
import os
import typing as typ
from pathlib import Path

from buildlimits import outcome_segments
from buildlimits.outcome_archive_fs import (
    atomic_write_json,
    fsync_directory,
    open_private_regular,
    open_private_temp,
    publish_no_clobber,
)
from buildlimits.outcome_archive_verify import (
    ArchiveFacts,
    SegmentFaultError,
    Trim,
    scan_archive,
    write_archive,
)
from buildlimits.outcome_record_stream import LineStream

if typ.TYPE_CHECKING:
    import datetime as dt

    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_segments import SegmentEntry, SegmentLayout, Sidecar

_DIRECTORY_MODE = 0o700
_QUARANTINE_TOKEN_BYTES = 6

type ActionName = typ.Literal[
    "archived",
    "reconciled",
    "re-archived",
    "trimmed",
    "deleted",
    "repaired",
    "quarantined",
]

__all__ = [
    "ActionName",
    "ActionResult",
    "ArchiveContext",
    "archive_source",
    "delete_archive",
    "quarantine_archive",
    "quarantine_file",
    "quarantine_source",
    "read_archive_facts",
    "read_source_facts",
    "recompress_archive",
    "reconcile_duplicate",
    "remove_orphan_sidecar",
    "repair_sidecar",
]


@dc.dataclass(frozen=True, slots=True)
class ArchiveContext:
    """The layout, policy and moment one archive run works with.

    Attributes
    ----------
    layout : SegmentLayout
        The store's segment layout.
    policy : RetentionPolicy
        The effective retention and headroom policy.
    preset : int
        The XZ preset used for new and rewritten archives.
    now : datetime
        The run's caller-supplied UTC wall time.
    """

    layout: SegmentLayout
    policy: RetentionPolicy
    preset: int
    now: dt.datetime


@dc.dataclass(frozen=True, slots=True)
class ActionResult:
    """What one segment operation did, for the run report.

    Attributes
    ----------
    action : ActionName
        The closed operation name.
    sequence : int
        The segment sequence.
    logical_bytes : int
        Logical bytes the operation produced; zero for a deletion.
    physical_bytes : int
        Bytes the archive occupies after the operation.
    reason : str | None
        A closed fault reason when the operation quarantined something.
    """

    action: ActionName
    sequence: int
    logical_bytes: int = 0
    physical_bytes: int = 0
    reason: str | None = None


@dc.dataclass(frozen=True, slots=True)
class _EncodeOptions:
    """The compression settings for one temporary archive."""

    preset: int
    trim: Trim
    is_compressed: bool


def archive_source(context: ArchiveContext, entry: SegmentEntry) -> ActionResult:
    """Compress one sealed source, publish it and remove the source.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    entry : SegmentEntry
        The sealed source to archive.

    Returns
    -------
    ActionResult
        The published archive's logical and physical sizes.
    """
    options = _EncodeOptions(preset=context.preset, trim=Trim(), is_compressed=False)
    temporary, facts = _build_temp(context, entry.path, entry.sequence, options)
    try:
        publish_no_clobber(temporary, _archive_path(context, entry.sequence))
    finally:
        _discard(temporary)
    sidecar = _write_sidecar(context, entry.sequence, facts)
    _unlink(entry.path)
    fsync_directory(context.layout.sealed_dir)
    return ActionResult(
        "archived", entry.sequence, sidecar.logical_bytes, sidecar.physical_bytes
    )


def recompress_archive(
    context: ArchiveContext,
    entry: SegmentEntry,
    trim: Trim,
) -> ActionResult:
    """Rewrite one archive with a prefix trim applied.

    The replacement is published by an atomic rename of a verified temporary
    file. A trim that keeps no records removes the archive instead, because an
    empty segment carries no history.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    entry : SegmentEntry
        The published archive to rewrite.
    trim : Trim
        The prefix policy to apply.

    Returns
    -------
    ActionResult
        The rewritten archive's sizes, or a deletion result.
    """
    options = _EncodeOptions(preset=context.preset, trim=trim, is_compressed=True)
    temporary, facts = _build_temp(context, entry.path, entry.sequence, options)
    try:
        if facts.record_count == 0:
            delete_archive(context, entry)
            return ActionResult("deleted", entry.sequence)
        Path(temporary).replace(entry.path)
        fsync_directory(entry.path.parent)
    finally:
        _discard(temporary)
    sidecar = _write_sidecar(context, entry.sequence, facts)
    return ActionResult(
        "trimmed", entry.sequence, sidecar.logical_bytes, sidecar.physical_bytes
    )


def repair_sidecar(context: ArchiveContext, entry: SegmentEntry) -> ActionResult:
    """Rebuild one missing or stale sidecar from its archive's content.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    entry : SegmentEntry
        The archive whose sidecar must be rebuilt.

    Returns
    -------
    ActionResult
        The repaired archive's verified sizes.
    """
    facts = read_archive_facts(entry.path)
    sidecar = _write_sidecar(context, entry.sequence, facts)
    return ActionResult(
        "repaired", entry.sequence, sidecar.logical_bytes, sidecar.physical_bytes
    )


def reconcile_duplicate(
    context: ArchiveContext,
    sealed: SegmentEntry,
    archived: SegmentEntry,
) -> ActionResult:
    """Settle a source whose archive was published before a crash.

    When the archive matches the source, the source is redundant and removed.
    An unreadable or mismatched archive is quarantined and the source archived
    again, so a later run cannot trust a half-published pair. A corrupt source
    is quarantined and the existing archive is kept.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    sealed : SegmentEntry
        The still-present sealed source.
    archived : SegmentEntry
        The archive sharing the source's sequence.

    Returns
    -------
    ActionResult
        Whether the pair was reconciled, re-archived or quarantined.
    """
    try:
        source = read_source_facts(sealed.path)
    except SegmentFaultError as fault:
        _quarantine_and_remove_sidecar(
            context, sealed.path, sealed.sequence, fault.reason
        )
        return ActionResult("quarantined", sealed.sequence, reason=fault.reason)
    try:
        existing = read_archive_facts(archived.path)
    except SegmentFaultError as fault:
        _quarantine_and_remove_sidecar(
            context, archived.path, archived.sequence, fault.reason
        )
        return _rearchive(context, sealed)
    if not source.matches(existing):
        _quarantine_and_remove_sidecar(
            context, archived.path, archived.sequence, "mismatch"
        )
        return _rearchive(context, sealed)
    _unlink(sealed.path)
    fsync_directory(context.layout.sealed_dir)
    return ActionResult(
        "reconciled",
        sealed.sequence,
        existing.logical_bytes,
        archived.physical_bytes,
    )


def delete_archive(context: ArchiveContext, entry: SegmentEntry) -> None:
    """Remove one archive and its sidecar and make the removal durable.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    entry : SegmentEntry
        The archive to remove.
    """
    _unlink(entry.path)
    _unlink(context.layout.archive_dir / outcome_segments.sidecar_name(entry.sequence))
    fsync_directory(context.layout.archive_dir)


def remove_orphan_sidecar(context: ArchiveContext, sequence: int) -> ActionResult:
    """Quarantine a sidecar that has no archive beside it.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    sequence : int
        The sequence whose sidecar is orphaned.

    Returns
    -------
    ActionResult
        The quarantine result.
    """
    path = context.layout.archive_dir / outcome_segments.sidecar_name(sequence)
    quarantine_file(context, path, sequence, "orphan_sidecar")
    return ActionResult("quarantined", sequence, reason="orphan_sidecar")


def quarantine_archive(
    context: ArchiveContext,
    entry: SegmentEntry,
    reason: str,
) -> ActionResult:
    """Quarantine one unusable archive and drop its sidecar.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    entry : SegmentEntry
        The archive to quarantine.
    reason : str
        The closed fault reason recorded in the quarantine name.

    Returns
    -------
    ActionResult
        The quarantine result, carrying the fault reason.
    """
    _quarantine_and_remove_sidecar(context, entry.path, entry.sequence, reason)
    return ActionResult("quarantined", entry.sequence, reason=reason)


def quarantine_source(
    context: ArchiveContext,
    entry: SegmentEntry,
    reason: str,
) -> ActionResult:
    """Quarantine one unusable sealed source.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    entry : SegmentEntry
        The source to quarantine.
    reason : str
        The closed fault reason recorded in the quarantine name.

    Returns
    -------
    ActionResult
        The quarantine result, carrying the fault reason.
    """
    quarantine_file(context, entry.path, entry.sequence, reason)
    return ActionResult("quarantined", entry.sequence, reason=reason)


def quarantine_file(
    context: ArchiveContext,
    path: Path,
    sequence: int,
    reason: str,
) -> str:
    """Move one unusable file into the quarantine directory without clobbering.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    path : Path
        The file to move.
    sequence : int
        The sequence the file belongs to.
    reason : str
        The closed fault reason recorded in the name.

    Returns
    -------
    str
        The quarantine file's name.
    """
    directory = context.layout.quarantine_dir
    directory.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
    directory.chmod(_DIRECTORY_MODE)
    token = os.urandom(_QUARANTINE_TOKEN_BYTES).hex()
    destination = directory / f"{path.name}.{reason}.{token}"
    Path(path).replace(destination)
    fsync_directory(directory)
    fsync_directory(path.parent)
    return destination.name


def read_archive_facts(path: Path) -> ArchiveFacts:
    """Decompress one archive to EOF and return what it actually holds.

    Parameters
    ----------
    path : Path
        The XZ archive to read.

    Returns
    -------
    ArchiveFacts
        The verified counts, span and digest.
    """
    descriptor, _metadata = open_private_regular(path, os.O_RDONLY, enforce_mode=False)
    with os.fdopen(descriptor, "rb") as source:
        return scan_archive(LineStream(source, is_compressed=True))


def read_source_facts(path: Path) -> ArchiveFacts:
    """Read one plain source to EOF and return what it actually holds.

    Parameters
    ----------
    path : Path
        The sealed JSON Lines source.

    Returns
    -------
    ArchiveFacts
        The verified counts, span and digest.
    """
    descriptor, _metadata = open_private_regular(path, os.O_RDONLY, enforce_mode=False)
    with os.fdopen(descriptor, "rb") as source:
        return scan_archive(LineStream(source))


def _build_temp(
    context: ArchiveContext,
    source_path: Path,
    sequence: int,
    options: _EncodeOptions,
) -> tuple[Path, ArchiveFacts]:
    """Write and verify one temporary archive beside its final name.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    source_path : Path
        The plain or compressed document to read.
    sequence : int
        The sequence used in the temporary file's prefix.
    options : _EncodeOptions
        The preset, prefix trim and source encoding.

    Returns
    -------
    tuple[Path, ArchiveFacts]
        The verified temporary file and the facts it holds.

    Raises
    ------
    SegmentFaultError
        If the temporary archive does not decompress to the bytes written.
    """
    descriptor, temporary = open_private_temp(
        context.layout.archive_dir, prefix=outcome_segments.segment_name(sequence)
    )
    try:
        written = _write_temp(source_path, descriptor, options)
        scanned = read_archive_facts(temporary)
    except BaseException:
        _discard(temporary)
        raise
    if not written.matches(scanned):
        _discard(temporary)
        raise SegmentFaultError("truncated_xz")
    return temporary, scanned


def _write_temp(
    source_path: Path,
    descriptor: int,
    options: _EncodeOptions,
) -> ArchiveFacts:
    """Compress one source into an open temporary file and sync it.

    Parameters
    ----------
    source_path : Path
        The plain or compressed document to read.
    descriptor : int
        The writable descriptor of the temporary archive.
    options : _EncodeOptions
        The preset and prefix trim.

    Returns
    -------
    ArchiveFacts
        The facts describing the bytes written.
    """
    source_descriptor, _metadata = open_private_regular(
        source_path, os.O_RDONLY, enforce_mode=False
    )
    with (
        os.fdopen(source_descriptor, "rb") as source,
        os.fdopen(descriptor, "wb") as sink,
    ):
        facts = write_archive(
            LineStream(source, is_compressed=options.is_compressed),
            sink,
            preset=options.preset,
            trim=options.trim,
        )
        sink.flush()
        os.fsync(sink.fileno())
    return facts


def _write_sidecar(
    context: ArchiveContext, sequence: int, facts: ArchiveFacts
) -> Sidecar:
    """Describe one published archive and write its sidecar durably.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    sequence : int
        The archived sequence.
    facts : ArchiveFacts
        The verified facts of the published archive.

    Returns
    -------
    Sidecar
        The sidecar that was written.
    """
    archive_path = _archive_path(context, sequence)
    sidecar = outcome_segments.Sidecar(
        sequence=sequence,
        salt_id=facts.salt_id,
        record_count=facts.record_count,
        logical_bytes=facts.logical_bytes,
        physical_bytes=archive_path.lstat().st_size,
        sha256=facts.sha256,
        first_observed_at=facts.first_observed_at,
        last_observed_at=facts.last_observed_at,
        archived_at=context.now,
        xz_preset=context.preset,
        had_torn_tail=facts.had_torn_tail,
    )
    atomic_write_json(
        context.layout.archive_dir / outcome_segments.sidecar_name(sequence),
        sidecar.as_dict(),
    )
    return sidecar


def _rearchive(context: ArchiveContext, sealed: SegmentEntry) -> ActionResult:
    """Discard a stale archive and archive its source again.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    sealed : SegmentEntry
        The source to archive.

    Returns
    -------
    ActionResult
        The re-archived result.
    """
    context.layout.archive_dir.joinpath(
        outcome_segments.sidecar_name(sealed.sequence)
    ).unlink(missing_ok=True)
    result = archive_source(context, sealed)
    return dc.replace(result, action="re-archived")


def _quarantine_and_remove_sidecar(
    context: ArchiveContext,
    path: Path,
    sequence: int,
    reason: str,
) -> str:
    """Quarantine one file and drop the sidecar that described it.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    path : Path
        The unusable file.
    sequence : int
        The sequence the file belongs to.
    reason : str
        The closed fault reason.

    Returns
    -------
    str
        The quarantine file's name.
    """
    name = quarantine_file(context, path, sequence, reason)
    _unlink(context.layout.archive_dir / outcome_segments.sidecar_name(sequence))
    fsync_directory(context.layout.archive_dir)
    return name


def _archive_path(context: ArchiveContext, sequence: int) -> Path:
    """Return the final archive path for one sequence.

    Returns
    -------
    Path
        ``segment-<sequence>.jsonl.xz`` inside the archive directory.
    """
    return context.layout.archive_dir / outcome_segments.archive_name(sequence)


def _unlink(path: Path) -> None:
    """Remove one file, tolerating one that is already gone."""
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def _discard(path: Path) -> None:
    """Remove one abandoned temporary file, tolerating any race."""
    with contextlib.suppress(OSError):
        path.unlink()
