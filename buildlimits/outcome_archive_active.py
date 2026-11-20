"""Reading the writer's active file without taking the writer's queue.

The writer owns the active file and never holds the archiver's lock, so the
archiver reads one open version of it: the size and the rows come from the same
descriptor, and a writer that replaces the path mid-read cannot pair one
version's bytes with another's rows. A concurrent append can leave an
unterminated final row, which the bounded reader drops exactly as it drops a
torn tail from a sealed source. Only an absent file means there is no active
history; a path that exists but cannot be inspected is unknown history, not an
empty file.
"""

import dataclasses as dc
import errno
import os
import typing as typ

from buildlimits.outcome_archive_dirs import UnsafePathError
from buildlimits.outcome_archive_fs import open_private_regular
from buildlimits.outcome_archive_history import ActiveHistory
from buildlimits.outcome_archive_rows import (
    DocumentReader,
    HeaderRow,
    SegmentFaultError,
)
from buildlimits.outcome_record_stream import LineStream, StreamFaultError

if typ.TYPE_CHECKING:
    import datetime as dt
    from pathlib import Path

__all__ = ["active_history"]


def active_history(path: Path, *, cutoff: dt.datetime | None = None) -> ActiveHistory:
    """Read what the writer's active file holds at this moment.

    The store is scanned without the archive lock, because the writer owns
    that file and never holds the archiver's lock. A concurrent append can
    therefore leave an unterminated final row, which the reader drops exactly
    as the archiver drops a torn tail from a sealed source. Only an absent file
    means there is no active history: any other failure to inspect the path
    leaves its bytes and rows unknown, so it propagates rather than posing as
    an empty file.

    The file is opened once and measured through that descriptor, so its size
    and its rows always describe the same version even when the writer replaces
    the path with a compacted or sealed file while the scan runs.

    A path that is not a regular file is not the writer's store. Following a
    symlink would let a file outside the store decide retention, and the link's
    own metadata is not a history size; the writer and the bounded reader both
    refuse such a path, and so does this scan.

    Parameters
    ----------
    path : Path
        The active file the writer owns.
    cutoff : datetime | None
        The age cutoff the plan measures against, or None to count every row.
        With a cutoff, the scan also reports the rows and bytes the writer's
        own compaction keeps, so a budget never evicts closed history to make
        room for rows retention has already expired.

    Returns
    -------
    ActiveHistory
        The file's size in bytes, the record rows it currently holds and the
        span their observations cover. When a cutoff is given, the surviving
        figures describe the rows observed at or after it. A file that cannot
        be decoded is reported by its size alone, with no rows, so a record
        cap knows its count is unknown.

    Raises
    ------
    UnsafePathError
        If the path exists but is not a regular file, or another account owns
        it.
    OSError
        If the path cannot be inspected for any other reason, raised unchanged
        so the caller reports a failure rather than an empty history.
    """
    try:
        descriptor, metadata = open_private_regular(
            path, os.O_RDONLY, enforce_mode=False
        )
    except FileNotFoundError:
        return ActiveHistory()
    except OSError as error:
        if error.errno != errno.ELOOP:
            raise
        # ``O_NOFOLLOW`` refuses a symlink before it can name another file;
        # report it as the substituted path it is rather than a read failure.
        msg = "the active history path is not a regular file"
        raise UnsafePathError(msg, errno.EINVAL) from error
    size = metadata.st_size
    try:
        with os.fdopen(descriptor, "rb") as source:
            return _measure(LineStream(source), size=size, cutoff=cutoff)
    except OSError, SegmentFaultError, StreamFaultError:
        return ActiveHistory(bytes=size, has_metadata=False)


def _measure(
    stream: LineStream, *, size: int, cutoff: dt.datetime | None
) -> ActiveHistory:
    """Count one open active-file version and the rows the cutoff keeps.

    Returns
    -------
    ActiveHistory
        The file's size, every row it holds, and — when a cutoff is given —
        the bytes and rows the writer's own compaction would retain.

    Raises
    ------
    SegmentFaultError
        If the stream fails or the file does not begin with a header, so the
        caller reports the file as unmeasured rather than empty.
    """
    reader = DocumentReader(stream)
    records = 0
    surviving_records = 0
    surviving_bytes = 0
    first: dt.datetime | None = None
    last: dt.datetime | None = None
    saw_header = False
    try:
        for row in reader:
            if isinstance(row, HeaderRow):
                saw_header = True
                # The header always survives compaction, so it counts towards
                # the retained bytes even when every record row is expired.
                surviving_bytes += len(row.raw) + 1
                continue
            observed = row.record.observed_at
            records += 1
            first = observed if first is None else min(first, observed)
            last = observed if last is None else max(last, observed)
            if cutoff is None or observed >= cutoff:
                surviving_records += 1
                surviving_bytes += len(row.raw) + 1
    except StreamFaultError as fault:
        raise SegmentFaultError(fault.reason) from fault
    if not saw_header:
        raise SegmentFaultError("corrupt")
    history = ActiveHistory(
        bytes=size,
        records=records,
        first_observed_at=first,
        last_observed_at=last,
    )
    if cutoff is None:
        return history
    return dc.replace(
        history,
        surviving_bytes=surviving_bytes,
        surviving_records=surviving_records,
    )
