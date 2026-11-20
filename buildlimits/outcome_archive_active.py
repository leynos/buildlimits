"""Reading the writer's active file without taking the writer's queue.

The writer owns the active file and never holds the archiver's lock, so the
archiver reads it as a snapshot: a concurrent append can leave an unterminated
final row, which the bounded reader drops exactly as it drops a torn tail from
a sealed source. Only an absent file means there is no active history; a path
that exists but cannot be inspected is unknown history, not an empty file.
"""

import stat
import typing as typ

from buildlimits.outcome_archive_budget import ActiveHistory
from buildlimits.outcome_archive_dirs import UnsafePathError
from buildlimits.outcome_archive_encode import read_source_facts
from buildlimits.outcome_archive_verify import SegmentFaultError

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = ["active_history"]


def active_history(path: Path) -> ActiveHistory:
    """Read what the writer's active file holds at this moment.

    The store is scanned without the archive lock, because the writer owns
    that file and never holds the archiver's lock. A concurrent append can
    therefore leave an unterminated final row, which the reader drops exactly
    as the archiver drops a torn tail from a sealed source. Only an absent file
    means there is no active history: any other failure to inspect the path
    leaves its bytes and rows unknown, so it propagates rather than posing as
    an empty file.

    A path that is not a regular file is not the writer's store. Following a
    symlink would let a file outside the store decide retention, and the link's
    own metadata is not a history size; the writer and the bounded reader both
    refuse such a path, and so does this scan.

    Returns
    -------
    ActiveHistory
        The file's size in bytes, the record rows it currently holds and the
        span their observations cover. A file that cannot be decoded is
        reported by its size alone, with no rows, so a record cap knows its
        count is unknown.

    Raises
    ------
    UnsafePathError
        If the path exists but is not a regular file. Any other failure to
        inspect the path raises its ``OSError`` unchanged.
    """
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return ActiveHistory()
    if not stat.S_ISREG(metadata.st_mode):
        msg = "the active history path is not a regular file"
        raise UnsafePathError(msg)
    size = metadata.st_size
    try:
        facts = read_source_facts(path)
    except OSError, SegmentFaultError:
        return ActiveHistory(bytes=size, has_metadata=False)
    return ActiveHistory(
        bytes=size,
        records=facts.record_count,
        first_observed_at=facts.first_observed_at,
        last_observed_at=facts.last_observed_at,
    )
