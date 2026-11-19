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
from buildlimits.outcome_store_io import (
    ensure_directory,
    prepare_store,
    replace_temp,
    seal_active,
    store_size,
    write_metadata,
    write_sequence,
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


def seal(files: StoreFiles) -> bool:
    """Move the active file into the sealed directory and start a new one.

    Parameters
    ----------
    files : StoreFiles
        The writer's file state, updated in place.

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
        now = files.observe_clock()
        ensure_directory(files.sealed_dir)
        sequence = next_sequence(files.sequence_file, files.sealed_dir)
        metadata = sealed_metadata(files.records, sequence, salt.salt_id, now)
        destination = files.sealed_dir / outcome_segments.segment_name(sequence)
        write_sealing_state(files, sequence, destination, metadata)
        released = len(files.records)
        files.records = []
        files.header = outcome_records.StoreHeader(salt.salt_id, now)
        installed = restart_active(files)
    except OSError, ValueError, TypeError, UnicodeError, OverflowError:
        files.publish("seal", "save_failed")
        return False
    if not installed:
        files.publish("seal", "save_failed", records=released)
        return False
    files.needs_compaction = False
    files.clock_skew_pending = False
    files.publish("seal", "none", records=released)
    report_backlog(files)
    return True


def next_sequence(sequence_file: Path, sealed_dir: Path) -> int:
    """Choose the next sealed sequence after the counter and directory.

    Returns
    -------
    int
        One past every sequence already claimed.
    """
    try:
        recorded = read_sequence(sequence_file)
    except ValueError:
        recorded = None
    floor = 0 if recorded is None else recorded - 1
    return max(floor, highest_sealed(sealed_dir)) + 1


def highest_sealed(sealed_dir: Path) -> int:
    """Return the highest sequence name already in the sealed directory.

    Returns
    -------
    int
        The highest sequence, or zero when the directory is empty.
    """
    highest = 0
    try:
        iterator = sealed_dir.iterdir()
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
) -> None:
    """Publish the sequence and metadata, then move the active file.

    Raises
    ------
    RuntimeError
        If no active header exists to write into the fresh file.
    """
    write_sequence(files.sequence_file, sequence + 1)
    write_metadata(
        files.sealed_dir / outcome_segments.sealed_meta_name(sequence), metadata
    )
    if store_has_rows(files.path):
        seal_active(files.path, destination)
        return
    header = files.header
    if header is None:
        msg = "an active header is required to seal"
        raise RuntimeError(msg)
    replace_temp(prepare_store(files.path, header), files.path)


def store_has_rows(path: Path) -> bool:
    """Whether the active file holds anything worth sealing.

    Returns
    -------
    bool
        True when the active file exists and is non-empty.
    """
    try:
        return store_size(path) > 0
    except OSError:
        return False


def restart_active(files: StoreFiles) -> bool:
    """Install a fresh header-only active file.

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
    return True


def report_backlog(files: StoreFiles) -> None:
    """Report once each time the sealed directory reaches a backlog step."""
    try:
        count = sum(1 for _path in files.sealed_dir.iterdir())
    except OSError:
        return
    if count and count % ARCHIVE_BACKLOG_SEGMENTS == 0:
        files.publish("seal", "archive_backlog", records=count)
