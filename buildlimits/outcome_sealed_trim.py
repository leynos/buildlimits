"""Rewrite one sealed source in place to apply a retention trim.

A sealed source is plain JSON Lines, so a bound that asks for its leading rows
to be dropped can be applied without compressing it. The rewrite streams one
row at a time into a verified private temporary beside the source, preserves
each kept row's original bytes and newline, and publishes the result under the
sealed-rewrite marker: marker, rename, metadata, marker removal. A process that
stops anywhere in that sequence leaves a state the next run settles from the
file itself rather than from metadata that may describe bytes already replaced.

Working memory stays bounded by one row and one chunk whatever the source size,
and nothing here touches the active file the writer owns.
"""

import hashlib
import os
import typing as typ

from buildlimits import outcome_segment_sidecar, outcome_segments
from buildlimits.outcome_archive_allowance import composed_trim
from buildlimits.outcome_archive_context import ActionResult
from buildlimits.outcome_archive_encode import discard, read_source_facts
from buildlimits.outcome_archive_fs import (
    open_private_regular,
    open_private_temp,
    remove_private_files,
)
from buildlimits.outcome_archive_replace import (
    begin_sealed_replacement,
    end_sealed_replacement,
)
from buildlimits.outcome_archive_rows import (
    DocumentReader,
    HeaderRow,
    SegmentFaultError,
)
from buildlimits.outcome_archive_trim import Trim, TrimWalk
from buildlimits.outcome_archive_verify import ArchiveFacts, OutputFaultError
from buildlimits.outcome_record_stream import LineStream, StreamFaultError
from buildlimits.outcome_segment_removed import (
    record_removal,
    withdraw_if_survived,
)
from buildlimits.outcome_store_documents import (
    SealedRows,
    sealed_metadata_document,
    write_metadata,
)
from buildlimits.outcome_store_io import fsync_directory
from buildlimits.utc_text import optional_utc_moment

if typ.TYPE_CHECKING:
    import datetime as dt
    from pathlib import Path

    from buildlimits.outcome_archive_context import ArchiveContext
    from buildlimits.outcome_segment_inventory import SegmentEntry

__all__ = [
    "build_sealed_temp",
    "remove_sealed_files",
    "sealed_at_of",
    "trim_sealed_source",
    "write_sealed_metadata",
]


def build_sealed_temp(
    context: ArchiveContext,
    entry: SegmentEntry,
    trim: Trim,
) -> tuple[Path, ArchiveFacts]:
    """Write and verify one trimmed plain source beside its final name.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    entry : SegmentEntry
        The sealed source to rewrite.
    trim : Trim
        The prefix policy to apply.

    Returns
    -------
    tuple[Path, ArchiveFacts]
        The verified temporary file and the facts it holds; a temporary that
        does not scan back is refused by ``_verified_facts``.
    """
    descriptor, temporary = open_private_temp(
        context.layout.sealed_dir,
        prefix=outcome_segments.maintenance_prefix(entry.sequence),
    )
    try:
        written = _write_temp(entry.path, descriptor, trim)
    except BaseException:
        discard(temporary)
        raise
    return temporary, _verified_facts(temporary, written)


def _verified_facts(temporary: Path, written: ArchiveFacts) -> ArchiveFacts:
    """Prove one temporary scans back to the bytes this pass wrote.

    Returns
    -------
    ArchiveFacts
        The facts the temporary actually holds.

    Raises
    ------
    OutputFaultError
        If the temporary will not decode, ends in a torn tail, or describes
        different bytes. The source decoded cleanly in every case, so the
        fault names this run's output and the source must survive.
    """
    try:
        scanned = read_source_facts(temporary)
    except SegmentFaultError as fault:
        # The source was read to its end to write this temporary, so a
        # temporary that will not decode is this process's own output.
        discard(temporary)
        raise OutputFaultError("unverified_plain") from fault
    except BaseException:
        discard(temporary)
        raise
    if scanned.had_torn_tail or not written.matches(scanned):
        # Every row this pass wrote carries its terminator, so a torn tail or a
        # different count, size or digest means the temporary is not what this
        # process wrote.
        discard(temporary)
        raise OutputFaultError("unverified_plain")
    return scanned


def write_sealed_metadata(
    context: ArchiveContext,
    sequence: int,
    facts: ArchiveFacts,
    *,
    sealed_at: dt.datetime | None = None,
) -> None:
    """Describe one rewritten sealed source and install its metadata durably.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    sequence : int
        The sealed sequence being described.
    facts : ArchiveFacts
        The verified facts of the published source.
    sealed_at : datetime | None
        The moment the source was first sealed, preserved across a rewrite, or
        None to record the run's own moment.
    """
    document = sealed_metadata_document(
        sequence=sequence,
        salt_id=facts.salt_id,
        rows=SealedRows(
            record_count=facts.record_count,
            first_observed_at=facts.first_observed_at,
            last_observed_at=facts.last_observed_at,
        ),
        sealed_at=context.now if sealed_at is None else sealed_at,
    )
    write_metadata(
        context.layout.sealed_dir / outcome_segments.sealed_meta_name(sequence),
        document,
    )


def sealed_at_of(context: ArchiveContext, sequence: int) -> dt.datetime | None:
    """Read the moment one sealed source was first sealed, when it is known.

    An in-place trim only drops expired or excess rows, so the source's age is
    not reset by the rewrite. A metadata document that cannot be read leaves
    the seal time unknown, and the caller records the run's own moment.

    Returns
    -------
    datetime | None
        The recorded seal time, or None when it cannot be trusted.
    """
    path = context.layout.sealed_dir / outcome_segments.sealed_meta_name(sequence)
    document = outcome_segment_sidecar.read_mapping(path)
    if document is None:
        return None
    try:
        return optional_utc_moment(
            document.get("sealed_at"),
            label="sealed_at",
            message="sealed_at must be text",
        )
    except TypeError, ValueError:
        return None


def remove_sealed_files(directory: Path, sequence: int) -> None:
    """Remove one sealed source, its metadata and any unfinished marker.

    The three names are removed through one validated directory descriptor and
    one sync, so a crash cannot leave metadata describing a source whose rows
    are already gone.

    Parameters
    ----------
    directory : Path
        The sealed directory.
    sequence : int
        The sequence to remove.
    """
    remove_private_files(
        directory,
        [
            outcome_segments.segment_name(sequence),
            outcome_segments.sealed_meta_name(sequence),
            outcome_segments.sealed_marker_name(sequence),
        ],
    )


def trim_sealed_source(
    context: ArchiveContext,
    entry: SegmentEntry,
    trim: Trim,
) -> ActionResult:
    """Rewrite one sealed source in place with a prefix trim applied.

    The published file is installed only after a verified temporary exists, and
    the marker makes the window between the rename and the new metadata a state
    the next run repairs rather than trusts. A trim that keeps no records
    removes the source instead, because a header-only segment carries no
    history.

    Parameters
    ----------
    context : ArchiveContext
        The run's layout, policy and moment.
    entry : SegmentEntry
        The sealed source to rewrite.
    trim : Trim
        The prefix policy to apply.

    Returns
    -------
    ActionResult
        The rewritten source's sizes, or a deletion result when no records
        survive.

    Raises
    ------
    ValueError
        If the entry is not a sealed source. An archive is recompressed by
        ``outcome_archive_files.recompress_archive``.
    OSError
        If a file cannot be written or removed. A removal marker whose source
        survives the fault is withdrawn first.
    """
    if entry.state != "sealed":
        msg = "only a sealed source can be trimmed in place"
        raise ValueError(msg)
    sealed_at = sealed_at_of(context, entry.sequence)
    temporary, facts = build_sealed_temp(context, entry, trim)
    try:
        if facts.record_count == 0:
            # The removal is recorded before the unlink, so a process that
            # stops before the run installs its floor leaves evidence that
            # retention removed the sequence rather than losing it.
            record_removal(context.layout.archive_dir, entry.sequence)
            try:
                remove_sealed_files(context.layout.sealed_dir, entry.sequence)
            except OSError:
                withdraw_if_survived(
                    context.layout.archive_dir, entry.sequence, entry.path
                )
                raise
            return ActionResult("deleted", entry.sequence)
        begin_sealed_replacement(context.layout.sealed_dir, entry.sequence)
        temporary.replace(entry.path)
        fsync_directory(entry.path.parent)
        write_sealed_metadata(context, entry.sequence, facts, sealed_at=sealed_at)
    finally:
        discard(temporary)
    # Only now does the metadata describe the source that is published. Until
    # this point the marker keeps the previous document untrusted, so a process
    # that stops anywhere above is repaired from the source rather than
    # believed.
    end_sealed_replacement(context.layout.sealed_dir, entry.sequence)
    return ActionResult(
        "trimmed", entry.sequence, facts.logical_bytes, facts.logical_bytes
    )


def _write_temp(source_path: Path, descriptor: int, trim: Trim) -> ArchiveFacts:
    """Write one trimmed plain document and sync it.

    Returns
    -------
    ArchiveFacts
        The facts describing the bytes written.
    """
    with os.fdopen(descriptor, "wb") as sink:
        measured = composed_trim(source_path, trim, is_compressed=False)
        source_descriptor, _metadata = open_private_regular(
            source_path, os.O_RDONLY, enforce_mode=False
        )
        with os.fdopen(source_descriptor, "rb") as source:
            facts = _write_plain(LineStream(source), sink, measured)
            sink.flush()
            os.fsync(sink.fileno())
    return facts


def _write_plain(stream: LineStream, sink: typ.BinaryIO, trim: Trim) -> ArchiveFacts:
    """Copy the rows a trim keeps into ``sink`` as a plain JSON Lines document.

    The header is always written, and every kept row is written with its
    original bytes and a terminating newline, so the result is the document an
    archive of the same rows would decompress to.

    Returns
    -------
    ArchiveFacts
        Facts describing the bytes actually written.

    Raises
    ------
    SegmentFaultError
        If the source header or any kept row is invalid.
    """
    reader = DocumentReader(stream)
    written = _Written(sink)
    walk = TrimWalk(trim)
    try:
        for row in reader:
            if isinstance(row, HeaderRow):
                written.row(row.raw)
                continue
            if not walk.keep(row.raw, row.record.observed_at):
                continue
            written.row(row.raw)
            written.record(row.record.observed_at)
    except StreamFaultError as fault:
        raise SegmentFaultError(fault.reason) from fault
    if reader.header is None:
        raise SegmentFaultError("corrupt")
    return written.facts(reader.header.salt_id, had_torn_tail=reader.had_torn_tail)


class _Written:
    """Accumulate a plain document's bytes, counts and digest as they are written."""

    def __init__(self, sink: typ.BinaryIO) -> None:
        """Bind the sink and clear the counters."""
        self._sink = sink
        self._hasher = hashlib.sha256()
        self.logical_bytes = 0
        self.records = 0
        self._first: dt.datetime | None = None
        self._last: dt.datetime | None = None

    def row(self, raw: bytes) -> None:
        """Write one newline-terminated row and count its bytes."""
        data = raw + b"\n"
        self._sink.write(data)
        self._hasher.update(data)
        self.logical_bytes += len(data)

    def record(self, observed: dt.datetime) -> None:
        """Count one retained record row and widen the observed span."""
        self.records += 1
        self._first = observed if self._first is None else min(self._first, observed)
        self._last = observed if self._last is None else max(self._last, observed)

    def facts(self, salt_id: str, *, had_torn_tail: bool) -> ArchiveFacts:
        """Describe the bytes written.

        Returns
        -------
        ArchiveFacts
            The counts, size, digest and span of the written document.
        """
        return ArchiveFacts(
            salt_id=salt_id,
            record_count=self.records,
            logical_bytes=self.logical_bytes,
            sha256=self._hasher.hexdigest(),
            first_observed_at=self._first,
            last_observed_at=self._last,
            had_torn_tail=had_torn_tail,
        )
