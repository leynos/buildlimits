"""Per-segment file operations for the cache-outcome archiver.

Every operation here runs under the archive lock, never touches the active
file, and publishes only a verified temporary file. A sealed source is removed
after its archive, sidecar and directory are durable; an archive is replaced
only by an atomic rename of a verified temporary file. Failures name a closed
reason and leave the previous state readable.

The values shared by every operation live in ``outcome_archive_context``,
the quarantine moves in ``outcome_archive_quarantine`` and the temp/verify
machinery in ``outcome_archive_encode``.
"""

import contextlib
import dataclasses as dc
import typing as typ

from buildlimits import outcome_segments
from buildlimits.outcome_archive_context import ActionResult
from buildlimits.outcome_archive_encode import (
    EncodeOptions,
    build_temp,
    discard,
    read_archive_facts,
    read_source_facts,
    write_sidecar,
)
from buildlimits.outcome_archive_fs import publish_no_clobber
from buildlimits.outcome_archive_quarantine import quarantine_and_remove_sidecar
from buildlimits.outcome_archive_verify import SegmentFaultError, Trim
from buildlimits.outcome_store_io import fsync_directory

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_archive_context import ArchiveContext
    from buildlimits.outcome_segment_inventory import SegmentEntry

__all__ = [
    "archive_source",
    "delete_archive",
    "recompress_archive",
    "reconcile_duplicate",
    "repair_sidecar",
]


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
    options = EncodeOptions(preset=context.preset, trim=Trim(), is_compressed=False)
    temporary, facts = build_temp(context, entry.path, entry.sequence, options)
    try:
        publish_no_clobber(temporary, _archive_path(context, entry.sequence))
    finally:
        discard(temporary)
    sidecar = write_sidecar(context, entry.sequence, facts)
    _remove_sealed_source(context, entry.sequence)
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
    options = EncodeOptions(preset=context.preset, trim=trim, is_compressed=True)
    temporary, facts = build_temp(context, entry.path, entry.sequence, options)
    try:
        if facts.record_count == 0:
            delete_archive(context, entry)
            return ActionResult("deleted", entry.sequence)
        temporary.replace(entry.path)
        fsync_directory(entry.path.parent)
    finally:
        discard(temporary)
    sidecar = write_sidecar(context, entry.sequence, facts)
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
    sidecar = write_sidecar(context, entry.sequence, facts)
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
        quarantine_and_remove_sidecar(
            context, sealed.path, sealed.sequence, fault.reason
        )
        return ActionResult("quarantined", sealed.sequence, reason=fault.reason)
    try:
        existing = read_archive_facts(archived.path)
    except SegmentFaultError as fault:
        quarantine_and_remove_sidecar(
            context, archived.path, archived.sequence, fault.reason
        )
        return _rearchive(context, sealed)
    if not source.matches(existing):
        quarantine_and_remove_sidecar(
            context, archived.path, archived.sequence, "mismatch"
        )
        return _rearchive(context, sealed)
    _remove_sealed_source(context, sealed.sequence)
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


def _remove_sealed_source(context: ArchiveContext, sequence: int) -> None:
    """Remove one archived source and the writer metadata that described it.

    The sidecar is now the authoritative record of the segment's facts, so the
    writer's metadata would otherwise accumulate beside an empty directory.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    sequence : int
        The archived sequence whose source must be released.
    """
    _unlink(context.layout.sealed_dir / outcome_segments.segment_name(sequence))
    _unlink(context.layout.sealed_dir / outcome_segments.sealed_meta_name(sequence))
    fsync_directory(context.layout.sealed_dir)


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
