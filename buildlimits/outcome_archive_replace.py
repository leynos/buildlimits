"""The markers a rewrite leaves until its metadata is durable.

A rewrite publishes its replacement by an atomic rename and only then replaces
the metadata. A process that stops between those two steps leaves metadata
describing the bytes that used to be there. For an archive, compressed size
cannot tell the pair apart: two XZ streams of different content can occupy
exactly the same number of bytes. A sealed source has the same window but no
sidecar, so its marker covers the writer's metadata file instead. Either way
the rewrite records its intent before the rename. A scan that finds the marker
refuses the metadata and routes the artefact to the repair path, which
re-derives the facts from the file itself. The marker is removed only once
metadata describes the bytes that are actually published.
"""

import contextlib
import typing as typ

from buildlimits import outcome_segments
from buildlimits.outcome_archive_fs import atomic_write_json_prefixed
from buildlimits.outcome_store_io import fsync_directory

if typ.TYPE_CHECKING:
    from pathlib import Path


def _begin(directory: Path, name: str, sequence: int, *, prefix: str) -> Path:
    """Record that one file is about to be replaced by a durable marker.

    Returns
    -------
    Path
        The marker naming that sequence.
    """
    marker = directory / name
    atomic_write_json_prefixed(
        marker, {"schema": 1, "sequence": sequence}, prefix=prefix
    )
    return marker


def _end(directory: Path, name: str) -> None:
    """Remove one rewrite marker and make the removal durable."""
    with contextlib.suppress(FileNotFoundError):
        (directory / name).unlink()
    fsync_directory(directory)


__all__ = [
    "begin_replacement",
    "begin_sealed_replacement",
    "end_replacement",
    "end_sealed_replacement",
]


def begin_replacement(archive_dir: Path, sequence: int) -> Path:
    """Record that one archive is about to be replaced.

    Parameters
    ----------
    archive_dir : Path
        The archive directory the rewrite works in.
    sequence : int
        The sequence being rewritten.

    Returns
    -------
    Path
        The durable marker naming that sequence.
    """
    name = outcome_segments.replacing_name(sequence)
    return _begin(archive_dir, name, sequence, prefix=name)


def end_replacement(archive_dir: Path, sequence: int) -> None:
    """Remove one rewrite marker once its sidecar describes the archive.

    Parameters
    ----------
    archive_dir : Path
        The archive directory the rewrite worked in.
    sequence : int
        The sequence that is no longer being replaced.
    """
    _end(archive_dir, outcome_segments.replacing_name(sequence))


def begin_sealed_replacement(sealed_dir: Path, sequence: int) -> Path:
    """Record that one plain sealed source is about to be rewritten.

    Parameters
    ----------
    sealed_dir : Path
        The private directory the rewrite works in.
    sequence : int
        The sequence being rewritten.

    Returns
    -------
    Path
        The durable marker naming that sequence.
    """
    return _begin(
        sealed_dir,
        outcome_segments.sealed_marker_name(sequence),
        sequence,
        # The sealed directory is shared with the supervisor, so the marker's
        # temporary must carry the maintenance prefix the cleanup looks for.
        prefix=outcome_segments.maintenance_prefix(sequence),
    )


def end_sealed_replacement(sealed_dir: Path, sequence: int) -> None:
    """Remove one sealed rewrite marker once its metadata describes the file.

    Parameters
    ----------
    sealed_dir : Path
        The private directory the rewrite worked in.
    sequence : int
        The sequence that is no longer being rewritten.
    """
    _end(sealed_dir, outcome_segments.sealed_marker_name(sequence))
