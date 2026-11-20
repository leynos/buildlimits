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
from buildlimits.outcome_archive_quarantine import (
    quarantine_and_remove_sidecar,
    quarantine_source,
)
from buildlimits.outcome_archive_replace import begin_replacement, end_replacement
from buildlimits.outcome_archive_verify import ArchiveFacts, SegmentFaultError, Trim
from buildlimits.outcome_store_io import fsync_directory

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_archive_context import ArchiveContext
    from buildlimits.outcome_segment_inventory import SegmentEntry
    from buildlimits.outcome_segment_sidecar import Sidecar

__all__ = [
    "archive_source",
    "delete_archive",
    "delete_source",
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
    begin_replacement(context.layout.archive_dir, entry.sequence)
    try:
        if facts.record_count == 0:
            delete_archive(context, entry)
            return ActionResult("deleted", entry.sequence)
        temporary.replace(entry.path)
        fsync_directory(entry.path.parent)
        sidecar = _replace_sidecar(context, entry.sequence, facts)
    finally:
        discard(temporary)
    # Only now does the sidecar describe the archive that is published. Until
    # this point the marker keeps the old sidecar untrusted, so a process that
    # stops anywhere above is repaired from the archive rather than believed.
    end_replacement(context.layout.archive_dir, entry.sequence)
    return ActionResult(
        "trimmed", entry.sequence, sidecar.logical_bytes, sidecar.physical_bytes
    )


def _replace_sidecar(
    context: ArchiveContext, sequence: int, facts: ArchiveFacts
) -> Sidecar:
    """Replace one rewritten archive's sidecar, leaving no stale pair behind.

    The archive is already in place. If its sidecar cannot be written, the old
    sidecar describes bytes that no longer exist; removing it leaves the
    archive with no metadata, which the inventory reports as a missing sidecar
    and the next run repairs from the archive itself.

    Returns
    -------
    Sidecar
        The sidecar that was written.

    Raises
    ------
    OSError
        If the sidecar cannot be written and the stale one cannot be removed.
    """
    try:
        return write_sidecar(context, sequence, facts)
    except OSError:
        _unlink(context.layout.archive_dir / outcome_segments.sidecar_name(sequence))
        fsync_directory(context.layout.archive_dir)
        raise


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
    end_replacement(context.layout.archive_dir, entry.sequence)
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
        # The archive is intact; only its source is unreadable. Quarantining
        # the source must leave the archive's sidecar, which describes that
        # archive, in place.
        quarantine_source(context, sealed, fault.reason)
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
    if not archived.has_metadata:
        # A heal step skips duplicated sequences, so this is the last point at
        # which the verified archive's facts can be recorded before the source
        # that could regenerate them is removed. The sidecar is authoritative.
        write_sidecar(context, archived.sequence, existing)
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
    end_replacement(context.layout.archive_dir, entry.sequence)


def delete_source(context: ArchiveContext, entry: SegmentEntry) -> None:
    """Remove one sealed source the retention budget has evicted.

    A source old enough to be outside the retained history is removed without
    being compressed: compressing it first would only defer the same removal.
    The writer's metadata goes with it, exactly as it does after archival, so
    no sidecar outlives the rows it described.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    entry : SegmentEntry
        The sealed source to remove.
    """
    _remove_sealed_source(context, entry.sequence)


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
