"""Pin one consistent snapshot of the retained outcome history.

A history read must not mix rows from two states of the store. The pin
therefore lists the closed segments, resolves each one to the file that
actually holds its rows, fingerprints it and reads its header, and repeats
that work until a second scan agrees with the first. The active file is
copied whole, because the writer bounds it by the seal threshold.

Retention rewrites an archive through a rename and the writer seals the
active file the same way, so a replaced segment has a new identity. A pin
whose listed segment vanished, or whose header was read across a
replacement, is retried; a store that never settles fails closed as
``unsettled`` rather than pinning a shortened history.

A salt change partitions history: the default pins the newest domain, an
explicit identifier pins that domain, and an all-salts pin covers every
domain in append order without implying that their records are comparable.
"""

import dataclasses as dc
import io
import typing as typ

from buildlimits import outcome_records, outcome_segment_inventory
from buildlimits.outcome_archive_rows import (
    DocumentReader,
    HeaderRow,
    SegmentFaultError,
)
from buildlimits.outcome_history_settle import fingerprint, is_settled
from buildlimits.outcome_record_stream import LineStream, StreamFaultError
from buildlimits.outcome_segments import SegmentLayout, archive_name
from buildlimits.outcome_store_files import read_private_bytes

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_record_format import StoreHeader
    from buildlimits.outcome_segment_inventory import Inventory, SegmentEntry

# A writer that seals while a read runs is detected by the re-scan. Three
# attempts let a read settle across two seals without looping without bound.
HISTORY_ATTEMPTS = 3

__all__ = [
    "HISTORY_ATTEMPTS",
    "HistoryFaultError",
    "HistorySelection",
    "pin_history",
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


@dc.dataclass(frozen=True, slots=True)
class HistorySelection:
    """Which record domain a caller wants to read.

    Attributes
    ----------
    salt_id : str | None
        Read this salt domain, or the newest domain when None.
    all_salts : bool
        Read every domain in append order, ignoring ``salt_id``.
    """

    salt_id: str | None = None
    all_salts: bool = False


class _MovedError(Exception):
    """A pinned file changed while it was being read into the snapshot."""


@dc.dataclass(frozen=True, slots=True)
class _PinnedSegment:
    """One closed segment resolved and fingerprinted at pin time."""

    path: Path
    is_compressed: bool
    header: StoreHeader
    fingerprint: tuple[int, int, int]


@dc.dataclass(frozen=True, slots=True)
class _PinnedActive:
    """The writer's active file as the bytes the snapshot froze."""

    header: StoreHeader
    data: bytes


@dc.dataclass(frozen=True, slots=True)
class _Snapshot:
    """The immutable store view every pass of one stream reads."""

    segments: tuple[_PinnedSegment, ...]
    active: _PinnedActive | None
    selected: str | None
    header: StoreHeader
    salt_ids: tuple[str, ...]


def pin_history(
    layout: SegmentLayout, selection: HistorySelection | None = None
) -> _Snapshot:
    """Pin one consistent snapshot of the retained history.

    Parameters
    ----------
    layout : SegmentLayout
        The store's segment layout.
    selection : HistorySelection | None
        The record domain to read; the newest domain when None.

    Returns
    -------
    _Snapshot
        The frozen documents and the resolved selection.

    Raises
    ------
    FileNotFoundError
        If a settled scan finds no segment and no active file.
    HistoryFaultError
        If the store never settles, a listed segment vanished, a segment is
        corrupt, or an explicitly named salt was never written.
    """
    chosen = HistorySelection() if selection is None else selection
    for _attempt in range(HISTORY_ATTEMPTS):
        inventory = outcome_segment_inventory.read_inventory(layout)
        if _is_empty_store(layout, inventory):
            raise FileNotFoundError(layout.active)
        snapshot = _pinned_or_retry(layout, inventory, chosen)
        if snapshot is not None:
            return snapshot
    raise HistoryFaultError("unsettled")


def _pinned_or_retry(
    layout: SegmentLayout, inventory: Inventory, chosen: HistorySelection
) -> _Snapshot | None:
    """Pin one scanned version, or report that the store moved.

    Returns
    -------
    _Snapshot | None
        The pinned snapshot, or None when a fresh scan should be tried.

    Raises
    ------
    HistoryFaultError
        If a settled store cannot supply the snapshot: a listed segment
        vanished, or the selection names a salt the store does not carry.
    """
    try:
        snapshot = _pin(layout, inventory, chosen)
    except FileNotFoundError as error:
        # The archiver removed a segment between the scan and the pin. A retry
        # sees the archive it was replaced by; a store that never settles is a
        # fault, not empty history.
        if not is_settled(layout, inventory):
            return None
        raise HistoryFaultError("vanished") from error
    except _MovedError:
        return None
    except HistoryFaultError as error:
        # A salt that was active at the scan can be sealed before the pin reads
        # the active file, so the named domain appears to be missing while its
        # rows are retained in the new segment. Recheck the scan before
        # rejecting the selection; a settled store keeps the fault.
        if error.reason == "unknown_salt" and not is_settled(layout, inventory):
            return None
        raise
    return snapshot if is_settled(layout, inventory) else None


def _is_empty_store(layout: SegmentLayout, inventory: Inventory) -> bool:
    """Whether a settled scan found no history at all.

    The writer seals by renaming the active file into the sealed directory, so
    a seal between the scan and this check empties the active path while the
    rows live on in a segment the scan did not list. Only a store that a
    second scan still reports empty has no history; a changed one is left to
    the pin, whose missing active file sends it back for a fresh scan.

    Returns
    -------
    bool
        True when neither the scan nor the active path holds any history and
        a re-scan agrees with the scan.
    """
    if inventory.effective or layout.active.exists():
        return False
    return is_settled(layout, inventory)


def _pin(
    layout: SegmentLayout, inventory: Inventory, chosen: HistorySelection
) -> _Snapshot:
    """Resolve and fingerprint every document one settled scan listed.

    Returns
    -------
    _Snapshot
        The frozen documents and the resolved selection.

    Raises
    ------
    FileNotFoundError
        When the scan lists no history at all.

    Notes
    -----
    A listed segment that no longer exists propagates ``FileNotFoundError``,
    and a segment that changed while its header was read raises
    ``_MovedError``; both tell the caller to retry the scan. A selection that
    names a salt no segment carries raises ``HistoryFaultError``.
    """
    segments = tuple(_pin_segment(layout, entry) for entry in inventory.effective)
    active = _read_active(layout.active)
    if not segments and active is None:
        raise FileNotFoundError(layout.active)
    salt_ids = tuple(
        dict.fromkeys([
            *(segment.header.salt_id for segment in segments),
            *([] if active is None else [active.header.salt_id]),
        ])
    )
    selected, header = _resolution(chosen, segments, active)
    return _Snapshot(
        segments=segments,
        active=active,
        selected=selected,
        header=header,
        salt_ids=salt_ids,
    )


def _pin_segment(layout: SegmentLayout, entry: SegmentEntry) -> _PinnedSegment:
    """Resolve one closed entry to the file that actually holds its rows.

    Returns
    -------
    _PinnedSegment
        The resolved path, its header and its identity.

    Raises
    ------
    FileNotFoundError
        If neither the listed file nor its archive can be read.

    Notes
    -----
    A file that changes while its header is read raises ``_MovedError``.
    """
    try:
        return _pin_file(entry.path, is_compressed=entry.state == "archived")
    except FileNotFoundError:
        if entry.state == "archived":
            raise
        # The archiver may have removed this sealed source after publishing
        # its archive, which the scan did not list yet. The archive holds the
        # same rows, so pin it instead of silently shortening history.
        return _pin_file(
            layout.archive_dir / archive_name(entry.sequence), is_compressed=True
        )


def _pin_file(path: Path, *, is_compressed: bool) -> _PinnedSegment:
    """Read one file's header and pin its identity.

    Returns
    -------
    _PinnedSegment
        The file, its header and the identity read before and after the
        header.

    Raises
    ------
    _MovedError
        If the file's identity changed between the two reads, which means
        another process replaced it as it was pinned.
    """
    before = fingerprint(path)
    header = _read_header(path, is_compressed=is_compressed)
    if fingerprint(path) != before:
        raise _MovedError
    return _PinnedSegment(
        path=path,
        is_compressed=is_compressed,
        header=header,
        fingerprint=before,
    )


def _read_active(path: Path) -> _PinnedActive | None:
    """Freeze the active file's bytes, or report that it does not exist.

    The writer seals the active file at its segment threshold, so these bytes
    are bounded by that threshold rather than by the retained history. The
    read uses the store's bounded private-file reader so a symlink, a special
    file or a file that grew past the store bound is a closed fault instead of
    an unbounded read.

    Returns
    -------
    _PinnedActive | None
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
    return _PinnedActive(header=_header_of_bytes(data), data=data)


def _resolution(
    chosen: HistorySelection,
    segments: tuple[_PinnedSegment, ...],
    active: _PinnedActive | None,
) -> tuple[str | None, StoreHeader]:
    """Resolve a selection to the salt to yield and the header to report.

    Returns
    -------
    tuple[str | None, StoreHeader]
        The selected salt, or None for every domain, and the newest matching
        header.

    Raises
    ------
    HistoryFaultError
        If an explicitly named salt was never written.
    """
    newest = active.header if active is not None else segments[-1].header
    if chosen.all_salts:
        return None, newest
    if chosen.salt_id is None:
        return newest.salt_id, newest
    matching = [
        segment.header
        for segment in segments
        if segment.header.salt_id == chosen.salt_id
    ]
    if active is not None and active.header.salt_id == chosen.salt_id:
        matching.append(active.header)
    if not matching:
        raise HistoryFaultError("unknown_salt")
    return chosen.salt_id, matching[-1]


def _read_header(path: Path, *, is_compressed: bool) -> StoreHeader:
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
            return _header_of(LineStream(source, is_compressed=is_compressed))
    except (SegmentFaultError, StreamFaultError) as error:
        raise HistoryFaultError(str(error.reason)) from error


def _header_of_bytes(data: bytes) -> StoreHeader:
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
        return _header_of(LineStream(io.BytesIO(data)))
    except (SegmentFaultError, StreamFaultError) as error:
        raise HistoryFaultError(str(error.reason)) from error


def _header_of(stream: LineStream) -> StoreHeader:
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
