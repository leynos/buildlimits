"""The writer's file state machine for the cache-outcome store.

The writer thread owns exactly one ``StoreFiles`` value. Keeping the loading,
compaction and bounded-append rules here, apart from the queue plumbing in
``outcome_store``, lets the transition logic be read and tested without
starting a thread. Sealing lives in ``outcome_store_sealing``.
"""

import datetime as dt
import typing as typ

from buildlimits import (
    outcome_records,
    outcome_segments,
    outcome_store_restart,
    outcome_store_status,
)
from buildlimits.outcome_retention import RetentionPolicy
from buildlimits.outcome_store_io import (
    TornTailError,
    append_line,
    replace_store,
    store_size,
)
from buildlimits.outcome_store_read import read_store
from buildlimits.outcome_store_sealing import seal

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_archive_history import ActiveHistory
    from buildlimits.outcome_key import Salt
    from buildlimits.outcome_ports import StorePublisher as Publisher
    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.outcome_store_clock import Clocks
    from buildlimits.outcome_store_status import _Published

_CLOCK_SKEW_SECONDS = 24 * 60 * 60


class StoreFiles:
    """Own the active file, the retained rows and the clock-skew flag.

    Parameters
    ----------
    path : Path
        The JSON Lines store path, commonly returned by ``store_path``.
    publish : Publisher
        Sink for outcomes such as ``load``/``unreadable`` or ``compact``.
    clocks : Clocks
        Wall and monotonic clocks used for headers, retention and skew checks.
    policy : RetentionPolicy or None
        The effective retention policy. A policy that requires segmentation
        seals the active file at ``segment_bytes`` instead of compacting it in
        place; the legacy default keeps in-place compaction.

    Examples
    --------
    ``files.load_and_settle(salt, now)`` populates ``files.records`` from disk,
    then repairs, seals or compacts what it read, and leaves ``files.ready``
    true once the active file is within its bound.
    """

    def __init__(
        self,
        path: Path,
        publish: Publisher,
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
        self.oldest_observed: dt.datetime | None = None
        self.needs_compaction = False
        self.clock_skew_pending = False
        self.torn_tail = False
        self.ready = False
        self._published: _Published | None = None
        self._last_wall: dt.datetime | None = None
        self._last_monotonic: float | None = None

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

    def load_and_settle(self, salt: Salt, now: dt.datetime) -> None:
        """Load validated rows, initialize an empty store, then settle it.

        The name states the write: a startup that only read the file could be
        called ``load``, but this one may repair, seal or compact the store, so
        a reader that must stay side-effect free cannot use it.
        """
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

    def measure_active(self) -> int | None:
        """Return the active file's size, or None when it is unreadable.

        The method only reads. A caller that owns the operation the failure
        belongs to publishes the closed reason itself, so a name that says it
        measures cannot also notify an observer.

        Returns
        -------
        int or None
            The file's size, or ``None`` when it could not be measured.
        """
        try:
            return store_size(self.path)
        except OSError:
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
        """Publish the rows and byte size of one completed store state."""
        outcome_store_status.publish_snapshot(self)

    def retention(self) -> ActiveHistory:
        """Describe the rows the writer currently holds in the active file.

        Returns
        -------
        ActiveHistory
            The published active snapshot, or an incomplete one with a reason.
        """
        return outcome_store_status.retention_snapshot(self)

    def set_records(self, records: list[StoreRecord]) -> None:
        """Replace the active rows and cache their oldest observation.

        Retention asks whether the oldest retained row has aged past the
        cutoff, and a writer may append a row observed before every row it
        already holds. Caching the minimum here keeps that check constant-time
        however many rows the file carries.

        Parameters
        ----------
        records : list[StoreRecord]
            The rows the active file now holds.
        """
        self.records = records
        self.oldest_observed = min(
            (record.observed_at for record in records), default=None
        )

    def _accepts_append(self) -> bool:
        """Whether this writer can take another row after settling what it may.

        A writer with no header cannot append at all. One whose file needs
        compaction or has reached its record cap settles first: a seal that
        failed before its rename leaves rows the archiver cannot evict, so
        accepting another row would hold history past the cap.

        Returns
        -------
        bool
            Whether the writer is ready for the row.
        """
        if self.header is None:
            return False
        if self.needs_compaction and not self.settle():
            return False
        return not (self.over_record_cap() and not self.settle())

    def append(self, line: bytes, record: StoreRecord) -> None:
        """Compact at high water, preserve the hard bound and append one row."""
        if not self._accepts_append():
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
        except TornTailError:
            # The rollback that removes a partial line failed, so the file now
            # holds bytes this row list does not account for. Compaction
            # repairs them; until then the snapshot must not pair the two.
            self.needs_compaction = True
            self.torn_tail = True
            self.publish("append", "save_failed", discarded=1)
            return
        except OSError:
            self.needs_compaction = True
            self.publish("append", "save_failed", discarded=1)
            return
        self.records.append(record)
        if self.oldest_observed is None or record.observed_at < self.oldest_observed:
            self.oldest_observed = record.observed_at
        self.publish("append", "none", records=1)
        self.expire_if_aged()

    def over_record_cap(self) -> bool:
        """Whether the loaded active file already holds the record cap.

        Returns
        -------
        bool
            Whether a segmented store's active rows reach its configured cap.
        """
        policy = self.policy
        return (
            policy.is_segmented
            and policy.max_records is not None
            and len(self.records) >= policy.max_records
        )

    def expire_if_aged(self) -> bool:
        """Compact once the oldest retained row has aged past the cutoff.

        A segmented store seals only when the active file reaches
        ``segment_bytes``, so a quiet store would otherwise keep rows past the
        age bound until shutdown. Compaction drops every row observed before
        the cutoff, which makes the check self-limiting: it reads the clock on
        each append and rewrites the file only when the bound has actually
        been crossed. The check follows the oldest observation the file holds,
        not its first row. A caller may append a row observed before every row
        it follows, and that older row must not survive until some later row
        happens to age past the cutoff.

        Returns
        -------
        bool
            Whether the age bound rewrote the file.
        """
        policy = self.policy
        if not policy.has_age_limit or self.oldest_observed is None:
            return False
        cutoff = policy.cutoff(self.record_clock_observation())
        if cutoff is not None and self.oldest_observed < cutoff:
            return self.compact()
        return False

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
        size = self.measure_active()
        if size is None:
            self.publish("compact", "save_failed")
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
            now = self.record_clock_observation()
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
        self.set_records(list(compacted.records))
        self.clock_skew_pending = False
        self.needs_compaction = False
        self.torn_tail = False
        self.publish(
            "compact",
            compacted.store_reason,
            records=len(compacted.records),
            discarded=compacted.discarded,
        )
        return True

    def record_clock_observation(self) -> dt.datetime:
        """Read UTC time and retain a skew flag when wall time jumps by a day.

        The name states the effect: the reading becomes the baseline the next
        observation is compared with, and a jump of more than a day raises
        ``clock_skew_pending``, which makes the next compaction keep its rows
        rather than age them. A caller that only needs a timestamp reads
        ``clocks.wall()`` directly, which changes nothing.

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
