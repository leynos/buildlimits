"""Asynchronous writer for the bounded cache-outcome JSON Lines store.

``StoreWriter`` owns the queue and the worker thread; the file state machine it
drives lives in ``outcome_store_state`` and the segmented-mode transition in
``outcome_store_sealing``.
"""

import math
import queue
import threading
import time
import typing as typ

from buildlimits import outcome_records
from buildlimits.outcome_key import Salt
from buildlimits.outcome_ports import OutcomeStorePort, StoreFailure, StoreResult
from buildlimits.outcome_store_clock import Clocks
from buildlimits.outcome_store_salt import file_salt as file_salt
from buildlimits.outcome_store_salt import store_path as store_path
from buildlimits.outcome_store_state import StoreFiles

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt
    from pathlib import Path

    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_retention_report import ActiveHistory
    from buildlimits.outcome_vocabulary import StoreOperation, StoreReason

MAX_APPEND_QUEUE = 4_096
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
        self._salt_path = path.with_name("cache-outcomes.salt")
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
        self._files = StoreFiles(
            path,
            self._publish,
            Clocks(wall_clock, monotonic_clock),
            policy,
        )
        self._thread = threading.Thread(
            target=self._run, name="buildlimits-outcome-store", daemon=True
        )
        self._thread.start()

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

    # The writer is the port's adapter: status holds only the port, while the
    # rows stay owned by the worker-side state machine. Declaring the override
    # records that this forwarding *is* the port boundary, rather than an
    # indirection a reader could collapse.
    @typ.override
    def retention(self) -> ActiveHistory:
        """Describe the active file's retained rows for a status snapshot.

        Returns
        -------
        ActiveHistory
            The active count, logical size and observation span. A concurrent
            append is observed as a whole row or not at all.
        """
        return self._files.retention()

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
                if self._files.ready:
                    self._files.final_flush()
                return
            if not self._files.ready:
                self._publish("append", "save_failed", discarded=1)
                continue
            line, record = item
            self._files.append(line, record)

    def _initialize(self) -> None:
        """Acquire the salt, load bounded state and settle it before appending."""
        salt = self._read_initial_salt()
        if salt is None:
            return
        try:
            now = self._files.observe_clock()
        except TypeError, ValueError, OverflowError:
            self._publish("load", "unreadable")
            return
        self._files.load(salt, now)

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
                self._files.salt = active_salt
                self._publish("salt", "none")
                return active_salt

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
