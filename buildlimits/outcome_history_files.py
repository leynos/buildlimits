"""The files a history pin freezes, and the faults reading them raises.

A pin resolves each closed entry to the file that actually holds its rows,
fingerprints that file before and after reading its header, and copies the
writer's active file into the snapshot. A file another process replaced is
reported as moved so the pin can retry, and a file that cannot be read at all
is a closed history fault rather than an exception leaking out of the reader.
"""

import dataclasses as dc
import io
import typing as typ

from buildlimits import outcome_records
from buildlimits.outcome_archive_rows import (
    DocumentReader,
    HeaderRow,
    SegmentFaultError,
)
from buildlimits.outcome_history_settle import fingerprint
from buildlimits.outcome_record_stream import LineStream, StreamFaultError
from buildlimits.outcome_segments import archive_name
from buildlimits.outcome_store_files import read_private_bytes

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_record_format import StoreHeader
    from buildlimits.outcome_segment_inventory import SegmentEntry
    from buildlimits.outcome_segments import SegmentLayout

__all__ = [
    "HistoryFaultError",
    "MovedError",
    "PinnedActive",
    "PinnedSegment",
    "pin_segment",
    "read_active",
]


class HistoryFaultError(Exception):
    """A closed reason the retained history cannot be read.

    Parameters
    ----------
    reason : str
        The fault classification, never raw exception text.
    """

    def __init__(self, reason: str) -> None:
        """Retain the closed reason as the exception message."""
        super().__init__(reason)
        self.reason = reason


class MovedError(Exception):
    """A pinned file changed while it was being read into the snapshot."""


@dc.dataclass(frozen=True, slots=True)
class PinnedSegment:
    """One closed segment resolved and fingerprinted at pin time."""

    path: Path
    is_compressed: bool
    header: StoreHeader
    fingerprint: tuple[int, int, int]


@dc.dataclass(frozen=True, slots=True)
class PinnedActive:
    """The writer's active file as the bytes the snapshot froze."""

    header: StoreHeader
    data: bytes


def pin_segment(layout: SegmentLayout, entry: SegmentEntry) -> PinnedSegment:
    """Resolve one closed entry to the file that actually holds its rows.

    Returns
    -------
    PinnedSegment
        The resolved path, its header and its identity.

    Raises
    ------
    FileNotFoundError
        If neither the listed file nor its archive can be read.

    Notes
    -----
    A file that changes while its header is read raises ``MovedError``.
    """
    try:
        return pin_file(entry.path, is_compressed=entry.state == "archived")
    except FileNotFoundError:
        if entry.state == "archived":
            raise
        # The archiver may have removed this sealed source after publishing
        # its archive, which the scan did not list yet. The archive holds the
        # same rows, so pin it instead of silently shortening history.
        return pin_file(
            layout.archive_dir / archive_name(entry.sequence), is_compressed=True
        )


def pin_file(path: Path, *, is_compressed: bool) -> PinnedSegment:
    """Read one file's header and pin its identity.

    Returns
    -------
    PinnedSegment
        The file, its header and the identity read before and after the
        header.

    Raises
    ------
    MovedError
        If the file's identity changed between the two reads, which means
        another process replaced it as it was pinned.
    """
    before = fingerprint(path)
    header = read_header(path, is_compressed=is_compressed)
    if fingerprint(path) != before:
        raise MovedError
    return PinnedSegment(
        path=path,
        is_compressed=is_compressed,
        header=header,
        fingerprint=before,
    )


def read_active(path: Path) -> PinnedActive | None:
    """Freeze the active file's bytes, or report that it does not exist.

    The writer seals the active file at its segment threshold, so these bytes
    are bounded by that threshold rather than by the retained history. The
    read uses the store's bounded private-file reader so a symlink, a special
    file or a file that grew past the store bound is a closed fault instead of
    an unbounded read.

    Returns
    -------
    PinnedActive | None
        The bytes and header of the writer's file, when it exists.

    Raises
    ------
    HistoryFaultError
        If the file exists but does not start with a supported header, cannot
        be read as a private regular file, or exceeds ``MAX_STORE_BYTES``.
    """
    try:
        data = read_private_bytes(path, limit=outcome_records.MAX_STORE_BYTES)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise HistoryFaultError("corrupt") from error
    return PinnedActive(header=header_of_bytes(data), data=data)


def read_header(path: Path, *, is_compressed: bool) -> StoreHeader:
    """Read one file's header without decoding its rows.

    Returns
    -------
    StoreHeader
        The file's first row.

    Raises
    ------
    HistoryFaultError
        If the stream is invalid or starts with something other than a
        supported header.
    """
    try:
        with path.open("rb") as source:
            return header_of(LineStream(source, is_compressed=is_compressed))
    except (SegmentFaultError, StreamFaultError) as error:
        raise HistoryFaultError(str(error.reason)) from error


def header_of_bytes(data: bytes) -> StoreHeader:
    """Read a header from bytes already held in memory.

    Returns
    -------
    StoreHeader
        The data's first row.

    Raises
    ------
    HistoryFaultError
        If the data is invalid or starts with something other than a
        supported header.
    """
    try:
        return header_of(LineStream(io.BytesIO(data)))
    except (SegmentFaultError, StreamFaultError) as error:
        raise HistoryFaultError(str(error.reason)) from error


def header_of(stream: LineStream) -> StoreHeader:
    """Read the first row of one line stream as a header.

    Returns
    -------
    StoreHeader
        The stream's header.

    Raises
    ------
    HistoryFaultError
        If the stream is empty or does not start with a supported header.
    """
    for row in DocumentReader(stream):
        if isinstance(row, HeaderRow):
            return row.header
    raise HistoryFaultError("corrupt")
