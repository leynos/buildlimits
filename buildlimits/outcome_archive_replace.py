"""The marker one archive rewrite leaves until its sidecar is durable.

A rewrite publishes its replacement archive by an atomic rename and only then
replaces the sidecar. A process that stops between those two steps leaves a
sidecar describing the bytes that used to be there, and compressed size cannot
tell the pair apart: two XZ streams of different content can occupy exactly the
same number of bytes. The rewrite therefore records its intent beside the
archive before the rename. A scan that finds the marker refuses the sidecar and
routes the archive to the repair path, which re-derives the facts from the file
itself. The marker is removed only once a sidecar describes the archive that is
actually published.
"""

import contextlib
import typing as typ

from buildlimits import outcome_segments
from buildlimits.outcome_archive_fs import atomic_write_json
from buildlimits.outcome_store_io import fsync_directory

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = ["begin_replacement", "end_replacement"]


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
    marker = archive_dir / outcome_segments.replacing_name(sequence)
    atomic_write_json(marker, {"schema": 1, "sequence": sequence})
    return marker


def end_replacement(archive_dir: Path, sequence: int) -> None:
    """Remove one rewrite marker once its sidecar describes the archive.

    Parameters
    ----------
    archive_dir : Path
        The archive directory the rewrite worked in.
    sequence : int
        The sequence that is no longer being replaced.
    """
    with contextlib.suppress(FileNotFoundError):
        (archive_dir / outcome_segments.replacing_name(sequence)).unlink()
    fsync_directory(archive_dir)
