"""Asynchronous writer for the bounded cache-outcome JSON Lines store."""

import datetime as dt
import math
import queue
import threading
import time
import typing as typ

from buildlimits import outcome_records, outcome_segments
from buildlimits.outcome_key import Salt
from buildlimits.outcome_ports import OutcomeStorePort, StoreFailure, StoreResult
from buildlimits.outcome_retention import RetentionPolicy
from buildlimits.outcome_store_io import append_line as _append_line
from buildlimits.outcome_store_io import ensure_directory as _ensure_directory
from buildlimits.outcome_store_io import prepare_store as _prepare_store
from buildlimits.outcome_store_io import read_store as _read_store
from buildlimits.outcome_store_io import replace_store as _replace_store
from buildlimits.outcome_store_io import replace_temp as _replace_temp
from buildlimits.outcome_store_io import seal_active as _seal_active
from buildlimits.outcome_store_io import store_size as _store_size
from buildlimits.outcome_store_io import write_metadata as _write_metadata
from buildlimits.outcome_store_io import write_sequence as _write_sequence
from buildlimits.outcome_store_salt import file_salt as file_salt
from buildlimits.outcome_store_salt import store_path as store_path
from buildlimits.utc_text import optional_utc_text, utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.outcome_vocabulary import StoreOperation, StoreReason

MAX_APPEND_QUEUE = 4_096
ARCHIVE_BACKLOG_SEGMENTS = 8
_CLOCK_SKEW_SECONDS = 24 * 60 * 60
_METADATA_SCHEMA = 1
_STOP = None


class StoreWriter(OutcomeStorePort):
    """Own store I/O on one worker behind non-blocking port methods.

    Parameters
    ----------
    path : Path
        The JSON Lines store path, commonly returned by ``store_path``.
    queue_capacity : int
        Maximum number of pending append records; D17 fixes production at 4096.
    wall_clock : Callable[[], datetime] or None
        UTC clock used by the file header and pure compactor.
    monotonic_clock : Callable[[], float]
        Monotonic clock paired with wall time to detect a large clock jump.
    policy : RetentionPolicy or None
        The effective retention policy. A policy that requires segmentation
        makes the writer seal the active file at ``segment_bytes`` instead of
        compacting it in place; the legacy default keeps in-place compaction.

    Examples
    --------
    ``writer = StoreWriter(path)`` accepts appends without doing file I/O on
    the caller; ``writer.close(1.0)`` bounds the shutdown wait.
    """

    # The constructor is the one seam switchboard every caller configures, so it
    # names each collaborator separately rather than hiding it in a settings
    # object a reader would have to look up. The arity is the interface, not an
    # accident, so both limits are waived permanently.
    def __init__(  # ruff: ignore[too-many-arguments]  # one keyword per seam, permanent; pylint: disable=too-many-arguments
        self,
        path: Path,
        *,
        queue_capacity: int = MAX_APPEND_QUEUE,
        wall_clock: cabc.Callable[[], dt.datetime] | None = None,
        monotonic_clock: cabc.Callable[[], float] = time.monotonic,
        policy: RetentionPolicy | None = None,
    ) -> None:
        """Start the sole file-owning worker without waiting for its setup.

        Raises
        ------
        ValueError
            If the append queue capacity is not positive.
        """
        if queue_capacity < 1:
            msg = "append queue capacity must be positive"
            raise ValueError(msg)
        self._path = path
        self._salt_path = path.with_name("cache-outcomes.salt")
        self._configured_policy = policy
        self._sealed_dir = path.parent / outcome_segments.SEALED_DIR_NAME
        self._sequence_file = path.parent / outcome_segments.SEQUENCE_NAME
        self._queue_capacity = queue_capacity
        self._work: queue.Queue[tuple[bytes, StoreRecord] | None] = queue.Queue(
            maxsize=queue_capacity + 1
        )
        self._salt_results: queue.Queue[Salt | StoreFailure] = queue.Queue(maxsize=1)
        self._result_lock = threading.Lock()
        self._pending_results: dict[
            tuple[StoreOperation, StoreReason], tuple[int, int]
        ] = {}
        self._state_lock = threading.Lock()
        self._closing = False
        self._wall_clock = _utc_now if wall_clock is None else wall_clock
        self._monotonic_clock = monotonic_clock
        self._last_wall: dt.datetime | None = None
        self._last_monotonic: float | None = None
        self._clock_skew_pending = False
        self._salt: Salt | None = None
        self._header: outcome_records.StoreHeader | None = None
        self._records: list[StoreRecord] = []
        self._needs_compaction = False
        self._ready = False
        self._thread = threading.Thread(
            target=self._run, name="buildlimits-outcome-store", daemon=True
        )
        self._thread.start()

    @property
    def _policy(self) -> RetentionPolicy:
        """The configured policy, else the legacy module bounds.

        Building the legacy policy on each access keeps the documented
        ``outcome_records`` constants live: a caller that adjusts them still
        changes the writer's bounds, as the pre-policy implementation did.
        """
        if self._configured_policy is not None:
            return self._configured_policy
        return RetentionPolicy(
            max_age=dt.timedelta(days=outcome_records.RETENTION_DAYS),
            max_bytes=outcome_records.MAX_COMPACT_BYTES,
            max_records=outcome_records.MAX_COMPACT_RECORDS,
        )

    def append(self, record: StoreRecord) -> None:
        """Encode and enqueue one record without waiting for disk activity.

        Parameters
        ----------
        record : StoreRecord
            The typed outcome or window summary to append.

        Examples
        --------
        A full queue returns immediately and exposes ``save_failed`` in
        ``results()``.
        """
        try:
            line = outcome_records.encode_line(record).encode("utf-8")
        except TypeError, ValueError, UnicodeError, RecursionError:
            self._publish("append", "save_failed", discarded=1)
            return
        if len(line) > outcome_records.MAX_RECORD_BYTES:
            self._publish("append", "save_failed", discarded=1)
            return
        with self._state_lock:
            if self._closing or self._work.qsize() >= self._queue_capacity:
                self._publish("append", "save_failed", discarded=1)
                return
            try:
                self._work.put_nowait((line, record))
            except queue.Full:
                self._publish("append", "save_failed", discarded=1)

    def results(self) -> tuple[StoreResult, ...]:
        """Drain aggregated writer outcomes without waiting for I/O.

        Returns
        -------
        tuple[StoreResult, ...]
            Pending counts grouped by operation and closed reason.

        Examples
        --------
        An idle writer returns ``()``.
        """
        with self._result_lock:
            pending = self._pending_results
            self._pending_results = {}
        return tuple(
            StoreResult(operation, reason, records, discarded)
            for (operation, reason), (records, discarded) in pending.items()
        )

    def salt_result(self) -> Salt | StoreFailure | None:
        """Return the worker's one-shot salt handoff without waiting.

        Returns
        -------
        Salt or StoreFailure or None
            Startup key material, its closed failure, or no result yet.

        Examples
        --------
        Before worker startup completes, ``salt_result()`` returns ``None``.
        """
        try:
            return self._salt_results.get_nowait()
        except queue.Empty:
            return None

    def close(self, timeout: float) -> None:
        """Queue the final flush and join the worker within one second.

        Parameters
        ----------
        timeout : float
            Maximum caller wait, capped at D17's one-second stop bound.

        Examples
        --------
        ``close(1.0)`` gives the final compaction or seal one second to finish.
        """
        wait = min(max(timeout, 0.0), 1.0) if math.isfinite(timeout) else 0.0
        with self._state_lock:
            if not self._closing:
                self._closing = True
                self._work.put_nowait(_STOP)
        self._thread.join(wait)

    def _run(self) -> None:
        """Initialize once, then serialize every queued operation."""
        self._initialize()
        while True:
            item = self._work.get()
            if item is _STOP:
                if self._ready:
                    self._final_flush()
                return
            if not self._ready:
                self._publish("append", "save_failed", discarded=1)
                continue
            line, record = item
            self._append_record(line, record)

    def _initialize(self) -> None:
        """Acquire the salt, load bounded state and compact before appending."""
        salt = self._read_initial_salt()
        if salt is None:
            return
        try:
            now = self._observe_clock()
        except TypeError, ValueError, OverflowError:
            self._publish("load", "unreadable")
            return
        self._load_store(salt, now)

    def _read_initial_salt(self) -> Salt | None:
        """Publish the one-shot result of creating or loading the local salt.

        Returns
        -------
        Salt or None
            The active key, or no value when the salt is unavailable.
        """
        try:
            salt = file_salt(self._salt_path)
        except OSError, ValueError:
            salt = StoreFailure("salt_unavailable")
        self._salt_results.put_nowait(salt)
        match salt:
            case StoreFailure() as failure:
                self._publish("salt", failure.reason)
                return None
            case Salt() as active_salt:
                self._salt = active_salt
                self._publish("salt", "none")
                return active_salt

    def _load_store(self, salt: Salt, now: dt.datetime) -> None:
        """Load validated rows, initialize an empty store, then compact it."""
        try:
            decoded = _read_store(self._path)
        except FileNotFoundError:
            decoded = None
        except OSError, ValueError:
            self._publish("load", "unreadable")
            return
        self._restore_loaded_store(decoded, salt, now)
        if self._ready:
            self._settle_loaded_store()

    def _settle_loaded_store(self) -> None:
        """Apply the startup bound: compact, or seal an oversized segment."""
        if not self._policy.is_segmented:
            self._compact()
            return
        try:
            size = _store_size(self._path)
        except OSError:
            self._publish("load", "unreadable")
            return
        if size > self._policy.segment_bytes:
            self._seal()

    def _restore_loaded_store(
        self,
        decoded: outcome_records.DecodedStore | outcome_records.RecordFailure | None,
        salt: Salt,
        now: dt.datetime,
    ) -> None:
        """Choose which validated rows can belong to the active salt."""
        match decoded:
            case outcome_records.RecordFailure() as failure:
                if not self._move_aside():
                    self._publish("load", failure.reason)
                    return
                self._publish("load", failure.reason)
                self._header = outcome_records.StoreHeader(salt.salt_id, now)
                self._records = []
            case None:
                self._publish("load", "none")
                self._header = outcome_records.StoreHeader(salt.salt_id, now)
                self._records = []
            case outcome_records.DecodedStore() as store if (
                store.header.salt_id != salt.salt_id
            ):
                self._publish("load", "salt_changed", discarded=len(store.records))
                self._header = outcome_records.StoreHeader(salt.salt_id, now)
                self._records = []
            case outcome_records.DecodedStore() as store:
                self._publish("load", "none", records=len(store.records))
                self._header = store.header
                self._records = list(store.records)
        self._ready = True

    def _move_aside(self) -> bool:
        """Move one rejected store without replacing an earlier backup.

        Returns
        -------
        bool
            Whether the rejected file was moved aside.
        """
        backup = self._path.with_name(f"{self._path.name}.bad")
        try:
            backup.lstat()
        except FileNotFoundError:
            pass
        else:
            return False
        try:
            self._path.replace(backup)
        except OSError:
            return False
        return True

    def _append_record(self, line: bytes, record: StoreRecord) -> None:
        """Compact at high water, preserve the hard bound and append one row."""
        if self._header is None:
            self._publish("append", "save_failed", discarded=1)
            return
        if self._needs_compaction and not self._settle():
            self._publish("append", "save_failed", discarded=1)
            return
        try:
            size = _store_size(self._path)
            water_mark = self._settle_bytes()
            if size + len(line) + 1 > water_mark:
                self._settle()
                size = _store_size(self._path)
            if size + len(line) + 1 > outcome_records.MAX_STORE_BYTES:
                self._publish("append", "save_failed", discarded=1)
                return
            _append_line(self._path, line)
        except OSError:
            self._needs_compaction = True
            self._publish("append", "save_failed", discarded=1)
            return
        self._records.append(record)
        self._publish("append", "none", records=1)

    def _settle_bytes(self) -> int:
        """Return the active size that triggers compaction or sealing.

        Returns
        -------
        int
            The segment size in segmented mode, else the byte budget.
        """
        policy = self._policy
        if policy.is_segmented:
            return policy.segment_bytes
        return policy.max_bytes

    def _settle(self) -> bool:
        """Apply the settled bound by compacting or sealing.

        Returns
        -------
        bool
            Whether the active file was made within its bound.
        """
        if self._policy.is_segmented:
            return self._seal()
        return self._compact()

    def _final_flush(self) -> None:
        """Apply the closing bound, trimming in place when nothing was sealed."""
        if not self._policy.is_segmented:
            self._compact()
            return
        try:
            size = _store_size(self._path)
        except OSError:
            self._publish("compact", "save_failed")
            return
        if size <= self._policy.segment_bytes:
            if self._policy.has_age_limit:
                self._compact()
            return
        self._seal()

    def _seal(self) -> bool:
        """Move the active file into the sealed directory and start a new one.

        The sequence counter is written before the rename so a crash leaves
        an empty gap rather than two segments sharing a name. A crash after
        the rename but before the new header is installed leaves no active
        file, so the abandoned rows are still sealed and the next start opens
        a fresh one; the retained rows only have to outlive that restart.

        Returns
        -------
        bool
            Whether the sealed segment was installed and a new file opened.
        """
        salt = self._salt
        if self._header is None or salt is None:
            return False
        if not self._records:
            # Nothing has been appended since the last seal, so the active file
            # holds only a header. Claiming a sequence now would publish
            # metadata for a source that never exists and leave a permanent gap.
            # The header file still has to exist, because appends open it.
            return self._restart_active()
        try:
            now = self._observe_clock()
            _ensure_directory(self._sealed_dir)
            sequence = self._next_sequence()
            metadata = dict(self._metadata(sequence, salt, now))
            destination = self._sealed_dir / outcome_segments.segment_name(sequence)
            self._write_sealing_state(sequence, destination, metadata, now)
            released = len(self._records)
            self._records = []
            self._header = outcome_records.StoreHeader(salt.salt_id, now)
            installed = self._restart_active()
        except OSError, ValueError, TypeError, UnicodeError, OverflowError:
            self._publish("seal", "save_failed")
            return False
        if not installed:
            self._publish("seal", "save_failed", records=released)
            return False
        self._needs_compaction = False
        self._clock_skew_pending = False
        self._publish("seal", "none", records=released)
        self._report_backlog()
        return True

    def _next_sequence(self) -> int:
        """Choose the next sealed sequence after the counter and directory.

        Returns
        -------
        int
            One past every sequence already claimed.
        """
        try:
            recorded = outcome_segments.read_sequence(self._sequence_file)
        except ValueError:
            recorded = None
        highest = self._highest_sealed()
        floor = 0 if recorded is None else recorded - 1
        return max(floor, highest) + 1

    def _highest_sealed(self) -> int:
        """Return the highest sequence name already in the sealed directory.

        Returns
        -------
        int
            The highest sequence, or zero when the directory is empty.
        """
        highest = 0
        try:
            iterator = self._sealed_dir.iterdir()
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

    def _metadata(
        self, sequence: int, salt: Salt, now: dt.datetime
    ) -> dict[str, object]:
        """Describe the rows about to be sealed for the archiver and reports.

        Returns
        -------
        dict[str, object]
            Counts, salt identity and observed range, with no private values.
        """
        observed = [record.observed_at for record in self._records]
        return {
            "schema": _METADATA_SCHEMA,
            "sequence": sequence,
            "salt_id": salt.salt_id,
            "record_count": len(observed),
            "first_observed_at": optional_utc_text(min(observed, default=None)),
            "last_observed_at": optional_utc_text(max(observed, default=None)),
            "sealed_at": utc_text(now),
        }

    def _write_sealing_state(
        self,
        sequence: int,
        destination: Path,
        metadata: dict[str, object],
        now: dt.datetime,
    ) -> None:
        """Publish the sequence and metadata, then move the active file.

        Raises
        ------
        RuntimeError
            If no active header exists to write into the fresh file.
        """
        _write_sequence(self._sequence_file, sequence + 1)
        _write_metadata(
            self._sealed_dir / outcome_segments.sealed_meta_name(sequence), metadata
        )
        if self._store_has_rows():
            _seal_active(self._path, destination)
            return
        header = self._header
        if header is None:
            msg = "an active header is required to seal"
            raise RuntimeError(msg)
        _replace_temp(_prepare_store(self._path, header), self._path)

    def _store_has_rows(self) -> bool:
        """Whether the active file holds anything worth sealing.

        Returns
        -------
        bool
            True when the active file exists and is non-empty.
        """
        try:
            return _store_size(self._path) > 0
        except OSError:
            return False

    def _restart_active(self) -> bool:
        """Install a fresh header-only active file.

        Returns
        -------
        bool
            Whether the new active file was installed.
        """
        header = self._header
        if header is None:
            return False
        try:
            _replace_temp(_prepare_store(self._path, header), self._path)
        except OSError:
            return False
        return True

    def _report_backlog(self) -> None:
        """Report once each time the sealed directory reaches a backlog step."""
        try:
            count = sum(1 for _path in self._sealed_dir.iterdir())
        except OSError:
            return
        if count and count % ARCHIVE_BACKLOG_SEGMENTS == 0:
            self._publish("seal", "archive_backlog", records=count)

    def _compact(self) -> bool:
        """Apply retention, atomically replace the file and report success.

        Returns
        -------
        bool
            Whether the compacted store was installed successfully.
        """
        if self._header is None or self._salt is None:
            return False
        try:
            now = self._observe_clock()
            decoded = outcome_records.DecodedStore(self._header, tuple(self._records))
            compacted = outcome_records.compact(
                decoded,
                now=now,
                clock_skew=self._clock_skew_pending,
                policy=self._policy,
            )
            _replace_store(self._path, compacted.header, compacted.records)
        except OSError, ValueError, TypeError, UnicodeError, OverflowError:
            self._needs_compaction = True
            self._publish("compact", "save_failed")
            return False
        self._header = compacted.header
        self._records = list(compacted.records)
        self._clock_skew_pending = False
        self._needs_compaction = False
        self._publish(
            "compact",
            compacted.store_reason,
            records=len(compacted.records),
            discarded=compacted.discarded,
        )
        return True

    def _observe_clock(self) -> dt.datetime:
        """Read UTC time and retain a skew flag when wall time jumps by a day.

        Returns
        -------
        datetime
            The current aware UTC wall-clock value.
        """
        now = self._wall_clock()
        monotonic = self._monotonic_clock()
        if self._last_wall is not None and self._last_monotonic is not None:
            wall_elapsed = (now - self._last_wall).total_seconds()
            monotonic_elapsed = monotonic - self._last_monotonic
            if abs(wall_elapsed - monotonic_elapsed) > _CLOCK_SKEW_SECONDS:
                self._clock_skew_pending = True
        self._last_wall = now
        self._last_monotonic = monotonic
        return now

    def _publish(
        self,
        operation: StoreOperation,
        reason: StoreReason,
        *,
        records: int = 0,
        discarded: int = 0,
    ) -> None:
        """Coalesce results into a fixed operation/reason vocabulary map."""
        key = (operation, reason)
        with self._result_lock:
            previous = self._pending_results.get(key, (0, 0))
            self._pending_results[key] = (
                previous[0] + records,
                previous[1] + discarded,
            )


def _utc_now() -> dt.datetime:
    """Read the total system wall clock as an aware UTC value.

    Returns
    -------
    datetime
        The current aware UTC wall-clock value.
    """
    return dt.datetime.now(dt.UTC)
