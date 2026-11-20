"""Reading the writer's active file without taking the writer's queue.

The writer owns the active file and never holds the archiver's lock, so the
archiver reads one open version of it: the size and the rows come from the same
descriptor, and a writer that replaces the path mid-read cannot pair one
version's bytes with another's rows. A writer can also append while a scan
runs, so the size is read immediately before the scan and confirmed immediately
after it; a scan whose version changed is retried, and one that never settles
is unknown history. An append is committed by its newline, so an unterminated
final row is the writer's in-flight append, which an interrupted write rolls
back; it is excluded from the rows a budget counts. Only an absent file means
there is no active history; a path that exists but cannot be inspected is
unknown history, not an empty file.
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
    RecordRow,
    SegmentFaultError,
)
from buildlimits.outcome_record_stream import LineStream, StreamFaultError

if typ.TYPE_CHECKING:
    import datetime as dt
    from pathlib import Path

__all__ = ["active_history"]

# Scans retried when the writer appends while one runs. Three attempts cover an
# append during each scan; a file that never holds still is reported as unknown
# history, because pairing one version's size with another's rows would let a
# budget decide against a store that never existed.
_SCAN_ATTEMPTS = 3


def active_history(path: Path, *, cutoff: dt.datetime | None = None) -> ActiveHistory:
    """Read what the writer's active file holds at this moment.

    The store is scanned without the archive lock, because the writer owns
    that file and never holds the archiver's lock. An append is committed by
    its newline, so the reader drops an unterminated final row exactly as it
    drops a torn tail from a sealed source: the writer rolls such an append
    back when it fails, and a row it has not committed is not retained
    history. Only an absent file means there is no active history: any other
    failure to inspect the path leaves its bytes and rows unknown, so it
    propagates rather than posing as an empty file.

    The file is opened once and measured through that descriptor, so a writer
    that replaces the path with a compacted or sealed file while the scan runs
    cannot mix two versions. A writer that appends during the scan is detected
    by comparing the size before and after it; the scan is retried so both
    figures describe the same version, and a file that changes on every attempt
    is reported as unmeasured.

    A path that is not a regular file is not the writer's store. Following a
    symlink would let a file outside the store decide retention, and the link's
    own metadata is not a history size; the writer and the bounded reader both
    refuse such a path, and so does this scan.

    Parameters
    ----------
    path : Path
        The active file the writer owns.
    cutoff : datetime | None
        The age cutoff the plan measures against, or None to count every
        committed row. Either way the scan also reports the bytes and rows the
        writer's own compaction keeps, so a budget never evicts closed history
        to make room for an append the writer has not committed or for rows
        retention has already expired.

    Returns
    -------
    ActiveHistory
        The file's size in bytes, the committed record rows it currently
        holds and the span their observations cover, together with the bytes
        and rows the writer keeps: the header and every committed row, less
        those a cutoff has expired. A file that cannot be decoded, or one the
        writer changes on every scan, is reported by its size alone, with no
        rows and no metadata, so a record cap knows its count is unknown.

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
        descriptor, _metadata = open_private_regular(
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
    with os.fdopen(descriptor, "rb") as source:
        return _scan_settled(source, cutoff=cutoff)


def _scan_settled(source: typ.BinaryIO, *, cutoff: dt.datetime | None) -> ActiveHistory:
    """Measure one open file, retrying a version the writer changed.

    The size is taken immediately before the scan and confirmed immediately
    after it. A writer that appends between those readings is seen as a changed
    size, and the scan is repeated against the longer version rather than
    pairing its rows with the earlier size.

    Returns
    -------
    ActiveHistory
        The last consistent measurement, or the last size alone with no rows
        when no attempt measured a version that held still.
    """
    size = 0
    for _attempt in range(_SCAN_ATTEMPTS):
        size = _size_of(source)
        try:
            source.seek(0)
            history = _measure(LineStream(source), size=size, cutoff=cutoff)
        except OSError, SegmentFaultError, StreamFaultError:
            return ActiveHistory(
                bytes=_size_of(source, fallback=size), has_metadata=False
            )
        if _size_of(source, fallback=size) == size:
            return history
    return ActiveHistory(bytes=size, has_metadata=False)


def _size_of(source: typ.BinaryIO, *, fallback: int = 0) -> int:
    """Return the open file's current size.

    Returns
    -------
    int
        The descriptor's size, or ``fallback`` when it can no longer be read.
    """
    try:
        return os.fstat(source.fileno()).st_size
    except OSError, ValueError:
        return fallback


def _measure(
    stream: LineStream, *, size: int, cutoff: dt.datetime | None
) -> ActiveHistory:
    """Count one open active-file version and the rows the cutoff keeps.

    The reader holds back the final row until it knows whether the stream
    ended with a newline, because that row may still be an append in flight.
    Every earlier row is followed by another and is therefore committed.

    Returns
    -------
    ActiveHistory
        The file's size, every committed row it holds, and the bytes and rows
        the writer's own compaction keeps: the header plus committed rows,
        less those a cutoff has expired.

    Raises
    ------
    SegmentFaultError
        If the stream fails or the file does not begin with a header, so the
        caller reports the file as unmeasured rather than empty.
    """
    reader = DocumentReader(stream)
    tally = _Tally(cutoff)
    pending: RecordRow | None = None
    try:
        for row in reader:
            if isinstance(row, HeaderRow):
                tally.header(row)
                continue
            if pending is not None:
                tally.record(pending)
            pending = row
    except StreamFaultError as fault:
        raise SegmentFaultError(fault.reason) from fault
    if not tally.saw_header:
        raise SegmentFaultError("corrupt")
    # ``had_torn_tail`` is settled only once iteration ends. A final row
    # without its newline is the writer's append in flight; an interrupted
    # write truncates it back out, so it must not decide a budget.
    if pending is not None and not reader.had_torn_tail:
        tally.record(pending)
    return tally.as_history(size)


@dc.dataclass(slots=True)
class _Tally:
    """Accumulate one active-file scan, whole and surviving the cutoff.

    Attributes
    ----------
    cutoff : datetime | None
        The age cutoff reported rows and bytes must reach, or None to keep
        every committed row.
    records : int
        Committed record rows read, excluding the header.
    surviving_records : int
        Committed rows the writer keeps: those observed at or after the
        cutoff, or every committed row when there is none.
    surviving_bytes : int
        Header and committed-row bytes the writer keeps.
    first_observed_at : datetime | None
        Earliest committed row observation, when one was read.
    last_observed_at : datetime | None
        Latest committed row observation, when one was read.
    saw_header : bool
        Whether the document began with a decodable header.
    """

    cutoff: dt.datetime | None
    records: int = 0
    surviving_records: int = 0
    surviving_bytes: int = 0
    first_observed_at: dt.datetime | None = None
    last_observed_at: dt.datetime | None = None
    saw_header: bool = False

    def header(self, row: HeaderRow) -> None:
        """Count a header, which always survives compaction."""
        self.saw_header = True
        self.surviving_bytes += len(row.raw) + 1

    def record(self, row: RecordRow) -> None:
        """Count one committed record row against both figures."""
        observed = row.record.observed_at
        self.records += 1
        self.first_observed_at = (
            observed
            if self.first_observed_at is None
            else min(self.first_observed_at, observed)
        )
        self.last_observed_at = (
            observed
            if self.last_observed_at is None
            else max(self.last_observed_at, observed)
        )
        if self.cutoff is None or observed >= self.cutoff:
            self.surviving_records += 1
            self.surviving_bytes += len(row.raw) + 1

    def as_history(self, size: int) -> ActiveHistory:
        """Return the measured version as an immutable snapshot.

        The surviving figures are always reported, even without a cutoff: the
        descriptor size includes an append whose newline the writer has not
        written, and a budget that counted those bytes would evict closed
        history for a row the writer rolls back.

        Returns
        -------
        ActiveHistory
            The whole-file figures beside the header and committed rows the
            writer keeps.
        """
        return ActiveHistory(
            bytes=size,
            records=self.records,
            first_observed_at=self.first_observed_at,
            last_observed_at=self.last_observed_at,
            surviving_bytes=self.surviving_bytes,
            surviving_records=self.surviving_records,
        )
