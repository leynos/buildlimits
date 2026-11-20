"""Verified temporary archives and the sidecars that describe them.

A temporary archive is written beside its final name, decompressed again to
prove it reproduces exactly the bytes that were written, and only then handed
back to the caller for publication. Nothing here publishes a file: the caller
owns the atomic rename and the removal of the source.
"""

import contextlib
import dataclasses as dc
import os
import typing as typ

from buildlimits import outcome_segment_sidecar, outcome_segments
from buildlimits.outcome_archive_fs import (
    atomic_write_json,
    open_private_regular,
    open_private_temp,
)
from buildlimits.outcome_archive_verify import (
    ArchiveFacts,
    OutputFaultError,
    SegmentFaultError,
    Trim,
    scan_archive,
    write_archive,
)
from buildlimits.outcome_record_stream import LineStream

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_archive_context import ArchiveContext
    from buildlimits.outcome_segment_sidecar import Sidecar

__all__ = [
    "EncodeOptions",
    "build_temp",
    "discard",
    "read_archive_facts",
    "read_source_facts",
    "write_sidecar",
]


@dc.dataclass(frozen=True, slots=True)
class EncodeOptions:
    """The compression settings for one temporary archive.

    Attributes
    ----------
    preset : int
        The XZ preset used for the new archive.
    trim : Trim
        The prefix policy applied while copying rows.
    is_compressed : bool
        Whether the source is itself an XZ archive.
    """

    preset: int
    trim: Trim
    is_compressed: bool


def build_temp(
    context: ArchiveContext,
    source_path: Path,
    sequence: int,
    options: EncodeOptions,
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
    options : EncodeOptions
        The preset, prefix trim and source encoding.

    Returns
    -------
    tuple[Path, ArchiveFacts]
        The verified temporary file and the facts it holds.

    Raises
    ------
    OutputFaultError
        If the temporary archive cannot be decoded or does not decompress to
        the bytes written. The source was readable in both cases, so the fault
        names this run's output rather than the history it read.
    """
    descriptor, temporary = open_private_temp(
        context.layout.archive_dir, prefix=outcome_segments.segment_name(sequence)
    )
    try:
        written = _write_temp(source_path, descriptor, options)
    except BaseException:
        discard(temporary)
        raise
    try:
        scanned = read_archive_facts(temporary)
    except SegmentFaultError as fault:
        # The source was read to its end to write this temporary, so a
        # temporary that will not decompress is this process's own output.
        # Reporting it as a source fault would quarantine readable history.
        discard(temporary)
        raise OutputFaultError("truncated_xz") from fault
    except BaseException:
        discard(temporary)
        raise
    if not written.matches(scanned):
        # The source decoded cleanly, so the mismatch is this process's own
        # output. The caller must retry rather than quarantine the source.
        discard(temporary)
        raise OutputFaultError("truncated_xz")
    # The temporary is a complete archive, so scanning it can never see the
    # source's unterminated final row. Only the writing pass knows the source
    # was torn, and the sidecar is the run's only record of that repair.
    return temporary, dc.replace(scanned, had_torn_tail=written.had_torn_tail)


def _scan_facts(path: Path, *, is_compressed: bool) -> ArchiveFacts:
    """Scan one private document to EOF and return what it actually holds.

    Returns
    -------
    ArchiveFacts
        The verified counts, span and digest.
    """
    descriptor, _metadata = open_private_regular(path, os.O_RDONLY, enforce_mode=False)
    with os.fdopen(descriptor, "rb") as source:
        return scan_archive(LineStream(source, is_compressed=is_compressed))


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
    return _scan_facts(path, is_compressed=True)


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
    return _scan_facts(path, is_compressed=False)


def write_sidecar(
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
    archive_path = context.layout.archive_dir / outcome_segments.archive_name(sequence)
    sidecar = outcome_segment_sidecar.Sidecar(
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


def discard(path: Path) -> None:
    """Remove one abandoned temporary file, tolerating any race."""
    with contextlib.suppress(OSError):
        path.unlink()


def _write_temp(
    source_path: Path,
    descriptor: int,
    options: EncodeOptions,
) -> ArchiveFacts:
    """Compress one source into an open temporary file and sync it.

    Parameters
    ----------
    source_path : Path
        The plain or compressed document to read.
    descriptor : int
        The writable descriptor of the temporary archive.
    options : EncodeOptions
        The preset and prefix trim.

    Returns
    -------
    ArchiveFacts
        The facts describing the bytes written.
    """
    # The sink is opened first so the caller's descriptor is owned by a context
    # manager before anything else can fail. Opening the source rejects paths
    # that are not private regular files, and that rejection must not leak the
    # temporary's descriptor.
    with os.fdopen(descriptor, "wb") as sink:
        source_descriptor, _metadata = open_private_regular(
            source_path, os.O_RDONLY, enforce_mode=False
        )
        with os.fdopen(source_descriptor, "rb") as source:
            facts = write_archive(
                LineStream(source, is_compressed=options.is_compressed),
                sink,
                preset=options.preset,
                trim=options.trim,
            )
            sink.flush()
            os.fsync(sink.fileno())
    return facts
