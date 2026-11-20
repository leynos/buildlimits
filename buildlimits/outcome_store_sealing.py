"""Sealing the active outcome store into a closed, named segment.

Sealing is the writer's segmented-mode transition: it publishes the next
sequence and the segment metadata durably, renames the active file into the
sealed directory and installs a fresh header-only file. The sequence counter
is written before the rename so a crash leaves an empty gap rather than two
segments sharing a name.
"""

import typing as typ

from buildlimits import outcome_records, outcome_segments
from buildlimits.outcome_segment_state import read_sequence
from buildlimits.outcome_store_documents import write_metadata, write_sequence
from buildlimits.outcome_store_io import (
    ensure_directory,
    prepare_store,
    replace_temp,
    store_size,
    sync_file,
    sync_rename,
)
from buildlimits.utc_text import optional_utc_text, utc_text

if typ.TYPE_CHECKING:
    import datetime as dt
    from pathlib import Path

    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.outcome_store_state import StoreFiles

ARCHIVE_BACKLOG_SEGMENTS = 8
_METADATA_SCHEMA = 1

__all__ = ["ARCHIVE_BACKLOG_SEGMENTS", "seal"]


def seal(files: StoreFiles, *, salt_id: str | None = None) -> bool:
    """Move the active file into the sealed directory and start a new one.

    Parameters
    ----------
    files : StoreFiles
        The writer's file state, updated in place.
    salt_id : str or None
        The record domain the sealed rows belong to, defaulting to the active
        salt. A salt change passes the previous domain's identifier: the
        segment holds rows written before the change, so its metadata must
        name the salt those rows were keyed under, not the new one.

    Returns
    -------
    bool
        Whether the sealed segment was installed and a new file opened.
    """
    salt = files.salt
    if files.header is None or salt is None:
        return False
    if not files.records:
        # Nothing has been appended since the last seal, so the active file
        # holds only a header. Claiming a sequence now would publish metadata
        # for a source that never exists and leave a permanent gap. The header
        # file still has to exist, because appends open it.
        return restart_active(files)
    try:
        # A seal only needs a timestamp for its metadata. Reading the clock
        # port directly leaves the skew baseline and flag to the compaction
        # that owns them.
        now = files.clocks.wall()
        ensure_directory(files.sealed_dir)
        layout = outcome_segments.SegmentLayout.beside(files.path)
        sequence = next_sequence(
            files.sequence_file, files.sealed_dir, layout.archive_dir
        )
        domain = salt.salt_id if salt_id is None else salt_id
        metadata = sealed_metadata(files.records, sequence, domain, now)
        destination = files.sealed_dir / outcome_segments.segment_name(sequence)
        moved = write_sealing_state(files, sequence, destination, metadata)
    except OSError, ValueError, TypeError, UnicodeError, OverflowError:
        files.publish("seal", "save_failed")
        return False
    # The rows have left the active file, so the writer must stop treating
    # them as active even if the steps that follow fail.
    released = len(files.records)
    files.set_records([])
    files.header = outcome_records.StoreHeader(salt.salt_id, now)
    # The rename has already left the active file, so its two directory
    # entries are synced whatever happens next: a fresh active file that
    # cannot be installed must not also cost the store the source name that
    # now holds the released rows.
    synced = _sync_sealed(files) if moved else True
    if not (restart_active(files) and synced):
        files.publish("seal", "save_failed", records=released)
        return False
    files.needs_compaction = False
    files.clock_skew_pending = False
    files.publish("seal", "none", records=released)
    report_backlog(files)
    return True


def next_sequence(sequence_file: Path, sealed_dir: Path, archived_dir: Path) -> int:
    """Choose the next sealed sequence after the counter and both directories.

    The counter is authoritative when it is readable, but a missing or damaged
    counter falls back to the directory names. Archived names count too: a
    store whose sealed sources were all archived and whose counter was lost
    would otherwise restart at sequence one and collide with an existing
    archive, leaving the new source permanently unarchived.

    Parameters
    ----------
    sequence_file : Path
        The next-sequence counter.
    sealed_dir : Path
        The directory holding sealed plain segments.
    archived_dir : Path
        The directory holding published XZ archives.

    Returns
    -------
    int
        One past every sequence already claimed.

    Raises
    ------
    OverflowError
        If no sequence is left that the twelve-digit inventory can name. A
        seal that cannot produce a visible source is reported as a save
        failure rather than publishing an invisible one.
    """
    try:
        recorded = read_sequence(sequence_file)
    except ValueError:
        recorded = None
    # The counter records one past the sequence just sealed, so subtracting one
    # recovers the last name the writer used. An exhausted counter therefore
    # reaches the guard below even when retention has removed every directory
    # entry that could have supplied the same answer.
    floor = 0 if recorded is None else recorded - 1
    highest = max(
        floor,
        highest_segment_name(sealed_dir),
        highest_segment_name(archived_dir),
    )
    if highest >= outcome_segments.MAX_SEQUENCE:
        msg = "the segment sequence is exhausted"
        raise OverflowError(msg)
    return highest + 1


def highest_segment_name(directory: Path) -> int:
    """Return the highest ``segment-*`` sequence name in one directory.

    Returns
    -------
    int
        The highest sequence, or zero when the directory is empty or unreadable.
    """
    highest = 0
    try:
        iterator = directory.iterdir()
    except OSError:
        return highest
    for path in iterator:
        name = path.name
        if not name.startswith("segment-"):
            continue
        digits = name.removeprefix("segment-").split(".", 1)[0]
        if digits.isdecimal():
            highest = max(highest, int(digits))
    return highest


def sealed_metadata(
    records: list[StoreRecord], sequence: int, salt_id: str, now: dt.datetime
) -> dict[str, object]:
    """Describe the rows about to be sealed for the archiver and reports.

    Returns
    -------
    dict[str, object]
        Counts, salt identity and observed range, with no private values.
    """
    observed = [record.observed_at for record in records]
    return {
        "schema": _METADATA_SCHEMA,
        "sequence": sequence,
        "salt_id": salt_id,
        "record_count": len(observed),
        "first_observed_at": optional_utc_text(min(observed, default=None)),
        "last_observed_at": optional_utc_text(max(observed, default=None)),
        "sealed_at": utc_text(now),
    }


def write_sealing_state(
    files: StoreFiles,
    sequence: int,
    destination: Path,
    metadata: dict[str, object],
) -> bool:
    """Publish the sequence and metadata, then move the active file.

    Returns
    -------
    bool
        Whether the active file was renamed into the sealed directory. The
        caller resets its rows either way, but only a rename needs its
        directory entry synced afterwards.

    Raises
    ------
    RuntimeError
        If no active header exists to write into the fresh file.

    Notes
    -----
    The record-bearing file is synced before the rename, so the source's rows
    are durable before the name that publishes them. The rename is kept
    separate from the directory sync so the caller can reset its rows as soon
    as they have left the active file.
    """
    write_sequence(files.sequence_file, sequence + 1)
    write_metadata(
        files.sealed_dir / outcome_segments.sealed_meta_name(sequence), metadata
    )
    if store_has_rows(files.path):
        # Append never syncs, and a rename makes the name durable without
        # making the bytes it points at durable. Ordering the content sync
        # first means a power failure after a completed seal cannot leave
        # durable metadata describing a source whose rows were never written.
        # A sync or rename failure propagates as ``OSError`` and leaves the
        # source in place, so the caller can retry it.
        sync_file(files.path)
        files.path.replace(destination)
        return True
    header = files.header
    if header is None:
        msg = "an active header is required to seal"
        raise RuntimeError(msg)
    replace_temp(prepare_store(files.path, header), files.path)
    return False


def _sync_sealed(files: StoreFiles) -> bool:
    """Make a completed rename durable across both directories.

    Returns
    -------
    bool
        Whether both directory entries were synced.
    """
    try:
        sync_rename(files.sealed_dir, files.path.parent)
    except OSError:
        return False
    return True


def store_has_rows(path: Path) -> bool:
    """Whether the active file holds anything worth sealing.

    An inspection failure propagates ``OSError`` rather than reading as "no
    rows", which would let the caller replace a populated file with an empty
    one.

    Returns
    -------
    bool
        True when the active file exists and is non-empty.
    """
    return store_size(path) > 0


def restart_active(files: StoreFiles) -> bool:
    """Install a fresh header-only active file.

    The replacement carries nothing from the file it replaces, so any fragment
    a failed append left behind is gone with it and the writer's rows and byte
    size describe the new file exactly.

    Returns
    -------
    bool
        Whether the new active file was installed.
    """
    header = files.header
    if header is None:
        return False
    try:
        replace_temp(prepare_store(files.path, header), files.path)
    except OSError:
        return False
    files.torn_tail = False
    return True


def report_backlog(files: StoreFiles) -> None:
    """Report once each time the sealed directory reaches a backlog step."""
    try:
        count = sum(1 for _path in files.sealed_dir.iterdir())
    except OSError:
        return
    if count and count % ARCHIVE_BACKLOG_SEGMENTS == 0:
        files.publish("seal", "archive_backlog", records=count)
