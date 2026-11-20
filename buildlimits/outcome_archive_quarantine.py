"""Quarantine moves for files an archive run cannot trust.

A quarantine move is a rename into a private directory under a name that names
the closed fault reason and carries a random token, so a repeat failure never
clobbers the earlier evidence. Everything here runs under the archive lock.
"""

import contextlib
import os
import typing as typ

from buildlimits import outcome_segments
from buildlimits.outcome_archive_context import ActionResult
from buildlimits.outcome_archive_fs import ensure_private_directory
from buildlimits.outcome_store_io import fsync_directory

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_archive_context import ArchiveContext
    from buildlimits.outcome_segment_inventory import SegmentEntry

_QUARANTINE_TOKEN_BYTES = 6

__all__ = [
    "quarantine_archive",
    "quarantine_file",
    "quarantine_source",
    "remove_orphan_sidecar",
]


def quarantine_file(
    context: ArchiveContext,
    path: Path,
    reason: str,
) -> str:
    """Move one unusable file into the quarantine directory without clobbering.

    The destination is named from the file's own name, the fault reason and a
    random token, so the sequence is not part of it; a sidecar's sequence is
    handled by the caller that knows one exists.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    path : Path
        The file to move.
    reason : str
        The closed fault reason recorded in the name.

    Returns
    -------
    str
        The quarantine file's name.
    """
    directory = context.layout.quarantine_dir
    # A symlinked quarantine directory would let the move place the file
    # outside the archive tree and a chmod change an unrelated directory, so
    # it is validated exactly as the archive directory is.
    ensure_private_directory(directory)
    token = os.urandom(_QUARANTINE_TOKEN_BYTES).hex()
    destination = directory / f"{path.name}.{reason}.{token}"
    path.replace(destination)
    fsync_directory(directory)
    fsync_directory(path.parent)
    return destination.name


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
    quarantine_and_remove_sidecar(context, entry.path, entry.sequence, reason)
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
    quarantine_file(context, entry.path, reason)
    return ActionResult("quarantined", entry.sequence, reason=reason)


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
    quarantine_file(context, path, "orphan_sidecar")
    return ActionResult("quarantined", sequence, reason="orphan_sidecar")


def quarantine_and_remove_sidecar(
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
    name = quarantine_file(context, path, reason)
    quarantine_sidecar(context, sequence)
    return name


def quarantine_sidecar(context: ArchiveContext, sequence: int) -> None:
    """Drop one stale sidecar and make the removal durable."""
    path = context.layout.archive_dir / outcome_segments.sidecar_name(sequence)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    fsync_directory(context.layout.archive_dir)
