"""The writer's file state machine for the cache-outcome store.

The writer thread owns exactly one ``StoreFiles`` value. Keeping the loading,
compaction and bounded-append rules here, apart from the queue plumbing in
``outcome_store``, lets the transition logic be read and tested without
starting a thread. Sealing lives in ``outcome_store_sealing``.
"""

import dataclasses as dc
import datetime as dt
import typing as typ

from buildlimits import outcome_records, outcome_segments, outcome_store_restart
from buildlimits.outcome_retention import RetentionPolicy
from buildlimits.outcome_retention_report import ActiveHistory, measured_history
from buildlimits.outcome_store_io import (
    append_line,
    read_store,
    replace_store,
    store_size,
)
from buildlimits.outcome_store_sealing import seal

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_key import Salt
    from buildlimits.outcome_ports import StorePublisher
    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.outcome_store_clock import Clocks
    from buildlimits.outcome_vocabulary import StoreOperation, StoreReason

_CLOCK_SKEW_SECONDS = 24 * 60 * 60


@dc.dataclass(frozen=True, slots=True)
class _Published:
    """One completed store state, as the writer's worker observed it.

    Attributes
    ----------
    rows : list[StoreRecord]
        The row list that state held. The worker only appends to a list it has
        published before replacing it with a new one, so a reader can take the
        published prefix while the worker appends.
    count : int
        The number of rows the state held.
    logical_bytes : int | None
        The active file's size in that state, or None when it could not be
        measured.
    """

    rows: list[StoreRecord]
    count: int
    logical_bytes: int | None


class StoreFiles:
    """Own the active file, the retained rows and the clock-skew flag.

    Parameters
    ----------
    path : Path
        The JSON Lines store path, commonly returned by ``store_path``.
    publish : StorePublisher
        Sink for outcomes such as ``load``/``unreadable`` or ``compact``.
    clocks : Clocks
        Wall and monotonic clocks used for headers, retention and skew checks.
    policy : RetentionPolicy or None
        The effective retention policy. A policy that requires segmentation
        seals the active file at ``segment_bytes`` instead of compacting it in
        place; the legacy default keeps in-place compaction.

    Examples
    --------
    ``files.load(salt, now)`` populates ``files.records`` from disk and leaves
    ``files.ready`` true once the active file is within its bound.
    """

    def __init__(
        self,
        path: Path,
        publish: StorePublisher,
        clocks: Clocks,
        policy: RetentionPolicy | None = None,
    ) -> None:
        """Take the store's paths and policy, leaving the in-memory view empty."""
        self.path = path
        self.publish = publish
        self.clocks = clocks
        self._configured_policy = policy
        self.sealed_dir = path.parent / outcome_segments.SEALED_DIR_NAME
        self.sequence_file = path.parent / outcome_segments.SEQUENCE_NAME
        self.salt: Salt | None = None
        self.header: outcome_records.StoreHeader | None = None
        self.records: list[StoreRecord] = []
        self.needs_compaction = False
        self.clock_skew_pending = False
        self.ready = False
        self._last_wall: dt.datetime | None = None
        self._last_monotonic: float | None = None
        self._published: _Published | None = None

    @property
    def policy(self) -> RetentionPolicy:
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

    def load(self, salt: Salt, now: dt.datetime) -> None:
        """Load validated rows, initialize an empty store, then settle it."""
        try:
            decoded = read_store(self.path)
        except FileNotFoundError:
            decoded = None
        except OSError, ValueError:
            self.publish("load", "unreadable")
            return
        self.restore_loaded_store(decoded, salt, now)
        if self.ready:
            self.settle_loaded_store()

    def measure_active(
        self, operation: StoreOperation, reason: StoreReason
    ) -> int | None:
        """Return the active file's size, reporting ``reason`` when it is unreadable.

        Parameters
        ----------
        operation : str
            The reported operation the failure belongs to.
        reason : str
            The closed reason published when the file cannot be measured.

        Returns
        -------
        int or None
            The file's size, or ``None`` when it could not be measured.
        """
        try:
            return store_size(self.path)
        except OSError:
            self.publish(operation, reason)
            return None

    def settle_loaded_store(self) -> None:
        """Install the header, compact, or seal an oversized segment."""
        outcome_store_restart.settle_loaded_store(self)

    def restore_loaded_store(
        self,
        decoded: outcome_records.DecodedStore | outcome_records.RecordFailure | None,
        salt: Salt,
        now: dt.datetime,
    ) -> None:
        """Choose which validated rows can belong to the active salt."""
        outcome_store_restart.restore_loaded_store(self, decoded, salt, now)

    def publish_snapshot(self) -> None:
        """Publish the rows and byte size of one completed store state.

        The worker owns every mutation and calls this after each transition it
        completes, so one immutable pairing always describes one moment. A
        status read can then report a coherent view without opening the file,
        waiting for an append or seeing a row that reached disk but not the
        writer's rows.
        """
        try:
            size: int | None = store_size(self.path)
        except OSError:
            size = None
        self._published = _Published(self.records, len(self.records), size)

    def retention(self) -> ActiveHistory:
        """Describe the rows the writer currently holds in the active file.

        The supervisor asks for this snapshot while the worker may be
        appending. It reads the last state the worker published, so the row
        count and the byte size always describe the same state; an append that
        races the read is either wholly included in a later snapshot or left
        to the next one.

        Returns
        -------
        ActiveHistory
            The active row count, its logical size and observation span, or an
            incomplete snapshot with a closed reason.
        """
        published = self._published
        if published is None:
            return ActiveHistory(complete=False, reason="not_loaded")
        size = published.logical_bytes
        if size is None:
            return ActiveHistory(complete=False, reason="unreadable")
        if not self.ready:
            return ActiveHistory(
                logical_bytes=size, complete=False, reason="not_loaded"
            )
        # A published list only grows until the worker replaces it, and a
        # replaced list stops changing, so its published prefix is intact even
        # while an append lands. The slice copies the state the size belongs
        # to, not whatever the list holds by the time it is read.
        rows = tuple(published.rows[: published.count])
        return measured_history(rows, logical_bytes=size)

    def append(self, line: bytes, record: StoreRecord) -> None:
        """Compact at high water, preserve the hard bound and append one row."""
        if self.header is None:
            self.publish("append", "save_failed", discarded=1)
            return
        if self.needs_compaction and not self.settle():
            self.publish("append", "save_failed", discarded=1)
            return
        try:
            size = store_size(self.path)
            water_mark = self.settle_bytes()
            if size + len(line) + 1 > water_mark:
                self.settle()
                size = store_size(self.path)
            if size + len(line) + 1 > outcome_records.MAX_STORE_BYTES:
                self.publish("append", "save_failed", discarded=1)
                return
            append_line(self.path, line)
        except OSError:
            self.needs_compaction = True
            self.publish("append", "save_failed", discarded=1)
            return
        self.records.append(record)
        self.publish("append", "none", records=1)

    def settle_bytes(self) -> int:
        """Return the active size that triggers compaction or sealing.

        Returns
        -------
        int
            The segment size in segmented mode, else the byte budget.
        """
        if self.policy.is_segmented:
            return self.policy.segment_bytes
        return self.policy.max_bytes

    def settle(self) -> bool:
        """Apply the settled bound by compacting or sealing.

        Returns
        -------
        bool
            Whether the active file was made within its bound.
        """
        if self.policy.is_segmented:
            return seal(self)
        return self.compact()

    def final_flush(self) -> None:
        """Apply the closing bound, trimming in place when nothing was sealed."""
        if not self.policy.is_segmented:
            self.compact()
            return
        size = self.measure_active("compact", "save_failed")
        if size is None:
            return
        if size <= self.policy.segment_bytes:
            if self.policy.has_age_limit:
                self.compact()
            return
        seal(self)

    def compact(self) -> bool:
        """Apply retention, atomically replace the file and report success.

        Returns
        -------
        bool
            Whether the compacted store was installed successfully.
        """
        if self.header is None or self.salt is None:
            return False
        try:
            now = self.observe_clock()
            decoded = outcome_records.DecodedStore(self.header, tuple(self.records))
            compacted = outcome_records.compact(
                decoded,
                now=now,
                clock_skew=self.clock_skew_pending,
                policy=self.policy,
            )
            replace_store(self.path, compacted.header, compacted.records)
        except OSError, ValueError, TypeError, UnicodeError, OverflowError:
            self.needs_compaction = True
            self.publish("compact", "save_failed")
            return False
        self.header = compacted.header
        self.records = list(compacted.records)
        self.clock_skew_pending = False
        self.needs_compaction = False
        self.publish(
            "compact",
            compacted.store_reason,
            records=len(compacted.records),
            discarded=compacted.discarded,
        )
        return True

    def observe_clock(self) -> dt.datetime:
        """Read UTC time and retain a skew flag when wall time jumps by a day.

        Returns
        -------
        datetime
            The current aware UTC wall-clock value.
        """
        now = self.clocks.wall()
        monotonic = self.clocks.monotonic()
        if self._last_wall is not None and self._last_monotonic is not None:
            wall_elapsed = (now - self._last_wall).total_seconds()
            monotonic_elapsed = monotonic - self._last_monotonic
            if abs(wall_elapsed - monotonic_elapsed) > _CLOCK_SKEW_SECONDS:
                self.clock_skew_pending = True
        self._last_wall = now
        self._last_monotonic = monotonic
        return now
