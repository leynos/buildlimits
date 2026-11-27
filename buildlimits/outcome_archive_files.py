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
from buildlimits.outcome_archive_fs import publish_no_clobber, remove_private_files
from buildlimits.outcome_archive_replace import (
    begin_replacement,
    end_replacement,
    end_sealed_replacement,
)
from buildlimits.outcome_archive_trim import Trim
from buildlimits.outcome_sealed_trim import (
    remove_sealed_files,
    sealed_at_of,
    write_sealed_metadata,
)
from buildlimits.outcome_store_io import fsync_directory

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_archive_context import ArchiveContext
    from buildlimits.outcome_archive_verify import ArchiveFacts
    from buildlimits.outcome_segment_inventory import SegmentEntry
    from buildlimits.outcome_segment_sidecar import Sidecar

__all__ = [
    "archive_source",
    "delete_archive",
    "delete_source",
    "recompress_archive",
    "repair_sealed_metadata",
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

    Raises
    ------
    ValueError
        If the entry is not a published archive. A sealed source is rewritten
        by ``outcome_sealed_trim.trim_sealed_source``, and compressing it here
        would turn retention into archival.
    """
    if entry.state != "archived":
        msg = "only a published archive can be recompressed"
        raise ValueError(msg)
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
        remove_private_files(
            context.layout.archive_dir,
            [outcome_segments.sidecar_name(sequence)],
        )
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


def repair_sealed_metadata(
    context: ArchiveContext, entry: SegmentEntry
) -> ActionResult:
    """Rebuild one sealed source's metadata from the source itself.

    A source whose metadata is missing, unusable, or covered by an unfinished
    rewrite marker cannot be measured, so no age, byte or record decision can
    be made about it. Reading the plain document re-derives the counts and span
    the writer's own seal would have recorded, and the original seal time is
    kept when it is still readable.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    entry : SegmentEntry
        The sealed source whose metadata must be rebuilt.

    Returns
    -------
    ActionResult
        The repaired source's verified sizes.
    """
    facts = read_source_facts(entry.path)
    write_sealed_metadata(
        context,
        entry.sequence,
        facts,
        sealed_at=sealed_at_of(context, entry.sequence),
    )
    end_sealed_replacement(context.layout.sealed_dir, entry.sequence)
    return ActionResult(
        "repaired", entry.sequence, facts.logical_bytes, entry.physical_bytes
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
    remove_private_files(
        context.layout.archive_dir,
        [
            outcome_segments.archive_name(entry.sequence),
            outcome_segments.sidecar_name(entry.sequence),
        ],
    )
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


def _remove_sealed_source(context: ArchiveContext, sequence: int) -> None:
    """Remove one archived source and the writer metadata that described it.

    The sidecar is now the authoritative record of the segment's facts, so the
    writer's metadata would otherwise accumulate beside an empty directory. An
    unfinished maintenance marker goes with them: the source it protected is
    gone, and leaving it would make the next inventory report work that no
    longer exists.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    sequence : int
        The archived sequence whose source must be released.
    """
    remove_sealed_files(context.layout.sealed_dir, sequence)


def _archive_path(context: ArchiveContext, sequence: int) -> Path:
    """Return the final archive path for one sequence.

    Returns
    -------
    Path
        ``segment-<sequence>.jsonl.xz`` inside the archive directory.
    """
    return context.layout.archive_dir / outcome_segments.archive_name(sequence)
