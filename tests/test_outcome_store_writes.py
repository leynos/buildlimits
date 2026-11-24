"""Queue pressure, append faults and bounded shutdown for the store writer."""

import datetime as dt
from pathlib import Path
from unittest import mock

import outcome_store_helpers as store_helpers
import pytest
from outcome_store_clock import FIXED_NOW
from outcome_store_fault_helpers import stalled_append
from outcome_store_helpers import (
    append_and_wait,
    assert_failed_append,
    running_writer,
    salt_path,
    start_writer,
    stored_records,
    wait_for_results,
    wait_for_salt,
)
from outcome_test_helpers import STORE_EPOCH as NOW
from outcome_test_helpers import STORE_EPOCH as _NOW
from outcome_test_helpers import window_summary
from segment_test_helpers import document, history_policy, write_private

from buildlimits import (
    outcome_store,
    outcome_store_read,
    outcome_store_restart,
    outcome_store_state,
)
from buildlimits.outcome_key import Salt
from buildlimits.outcome_ports import StoreResult
from buildlimits.outcome_record_format import DecodedStore
from buildlimits.outcome_store_clock import Clocks
from buildlimits.outcome_store_salt import file_salt


def test_writer_reports_queue_overflow_without_waiting_for_the_worker(
    tmp_path: Path,
) -> None:
    """A full append queue reports save failure while the worker is stalled."""
    path = tmp_path / "cache-outcomes.jsonl"
    with running_writer(path, queue_capacity=1, wall_clock=lambda: _NOW) as writer:
        # The ready context owns the worker while the queue fills behind a stall.
        with stalled_append() as (entered, release):
            writer.append(window_summary())
            assert entered.wait(1.0), "the worker reached the delayed write"
            writer.append(window_summary())
            writer.append(window_summary())
            results = wait_for_results(writer, operation="append", reason="save_failed")
            release.set()
            writer.close(1.0)
            successful = tuple(
                result
                for result in writer.results()
                if result.operation == "append" and result.reason == "none"
            )

    assert StoreResult("append", "save_failed", 0, 1) in results, (
        "overflow is counted without blocking the caller"
    )
    assert sum(result.records for result in successful) == 2, (
        "both accepted records share the coalesced success count"
    )


def test_ready_writer_closes_after_a_startup_wait_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed test setup closes the worker it started."""
    path = tmp_path / "cache-outcomes.jsonl"
    writer = start_writer(path)
    close = mock.Mock(wraps=writer.close)
    monkeypatch.setattr(writer, "close", close)
    monkeypatch.setattr(
        store_helpers,
        "start_writer",
        lambda *_args, **_kwargs: writer,
    )

    failed_wait = mock.Mock(side_effect=RuntimeError("salt wait failed"))
    monkeypatch.setattr(store_helpers, "wait_for_salt", failed_wait)
    try:
        with pytest.raises(RuntimeError, match="salt wait failed"):
            store_helpers.ready_writer(path)
        close.assert_called_once_with(1.0)
    finally:
        writer.close(1.0)


def test_worker_survives_clock_overflow_during_startup_compaction(
    tmp_path: Path,
) -> None:
    """A failed clock read is reported without killing the store worker."""
    path = tmp_path / "cache-outcomes.jsonl"
    clock_calls = 0
    now = dt.datetime(2026, 10, 25, tzinfo=dt.UTC)

    def wall_clock() -> dt.datetime:
        """Overflow on the first compaction, then allow close to finish."""
        nonlocal clock_calls
        clock_calls += 1
        if clock_calls == 2:
            raise OverflowError("clock is outside the supported range")
        return now

    writer = outcome_store.StoreWriter(path, wall_clock=wall_clock)
    try:
        compact = wait_for_results(writer, operation="compact", reason="save_failed")
        assert StoreResult("compact", "save_failed", 0, 0) in compact, (
            "the overflow becomes a closed store failure"
        )
        record = window_summary()
        writer.append(record)
        append = wait_for_results(writer, operation="append", reason="none")
        assert StoreResult("append", "none", 1, 0) in append, (
            "the worker retries compaction before accepting a later append"
        )
    finally:
        writer.close(1.0)

    assert stored_records(path) == (record,), "the retried append remains readable"


def test_a_seal_reads_the_clock_without_recording_a_skew(tmp_path: Path) -> None:
    """A timestamp-only read never changes what the next compaction decides.

    ``record_clock_observation`` updates the comparison baseline and raises the
    skew flag, which makes the next compaction keep its rows. A seal only needs
    the time for its metadata, so it reads the clock port directly and leaves
    that decision to the compaction that owns it.
    """
    wall = [NOW]
    monotonic = [100.0]
    files = outcome_store_state.StoreFiles(
        tmp_path / "cache-outcomes.jsonl",
        mock.Mock(),
        Clocks(wall=lambda: wall[0], monotonic=lambda: monotonic[0]),
    )
    files.record_clock_observation()
    wall[0] += dt.timedelta(days=2)
    monotonic[0] += 1.0

    assert files.clocks.wall() == wall[0], "the read returns the wall clock"
    assert not files.clock_skew_pending, "a read-only call records no skew"

    files.record_clock_observation()
    assert files.clock_skew_pending, "the recording call retains the jump"


def test_measure_active_reports_no_failure_of_its_own(tmp_path: Path) -> None:
    """A measurement failure belongs to the caller that knows the operation."""
    publisher = mock.Mock()
    # A directory is not a regular store file, so the measurement refuses it.
    files = outcome_store_state.StoreFiles(tmp_path, publisher, Clocks())

    assert files.measure_active() is None, "a non-regular path cannot be measured"
    publisher.assert_not_called()


def test_a_tail_that_cannot_be_repaired_stops_every_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer that cannot repair its tail never appends above the fragment."""
    path = tmp_path / "cache-outcomes.jsonl"
    monkeypatch.setattr(
        outcome_store_restart,
        "drop_torn_tail",
        mock.Mock(side_effect=OSError("read-only file system")),
    )

    writer = outcome_store.StoreWriter(
        path, policy=history_policy(1 << 20), wall_clock=lambda: FIXED_NOW
    )
    try:
        wait_for_results(writer, operation="load", reason="save_failed")
        writer.append(window_summary())
        refused = wait_for_results(writer, operation="append", reason="save_failed")
    finally:
        writer.close(1.0)

    assert StoreResult("append", "save_failed", 0, 1) in refused, (
        "the row is refused rather than joined to the fragment"
    )
    assert not path.exists(), "nothing is written above an unrepairable fragment"


def test_an_expired_active_file_is_compacted_at_startup(tmp_path: Path) -> None:
    """A restart applies the age bound to rows that never crossed the size."""
    store = tmp_path / "cache-outcomes.jsonl"
    salt = file_salt(salt_path(store))
    assert isinstance(salt, Salt), "the fixture installs its own salt"
    expired = window_summary(observed_at=NOW - dt.timedelta(days=40))
    write_private(store, document((expired,), salt.salt_id))

    writer = outcome_store.StoreWriter(
        store, policy=history_policy(1 << 20), wall_clock=lambda: NOW
    )
    try:
        results = wait_for_results(writer, operation="compact", reason="expired")
    finally:
        writer.close(1.0)

    assert any(result.discarded == 1 for result in results), (
        "the expired row is counted as discarded"
    )
    decoded = outcome_store_read.read_store(store)
    assert isinstance(decoded, DecodedStore), "the compacted store decodes"
    assert decoded.records == (), "only the replacement header remains"


def test_a_row_that_arrives_expired_is_dropped_by_its_own_append(
    tmp_path: Path,
) -> None:
    """The age bound applies to ordinary appends, not only to startup.

    A quiet store never reaches its segment size, so waiting for the next
    restart would let an expired row outlive the bound indefinitely.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    expired = window_summary(observed_at=NOW - dt.timedelta(days=40))
    writer = outcome_store.StoreWriter(
        store, policy=history_policy(1 << 20), wall_clock=lambda: NOW
    )
    try:
        wait_for_salt(writer)
        wait_for_results(writer, operation="load")
        append_and_wait(writer, expired)
        compacted = wait_for_results(writer, operation="compact", reason="expired")
        decoded = outcome_store_read.read_store(store)
    finally:
        writer.close(1.0)

    assert any(result.discarded == 1 for result in compacted), (
        "the expired row is counted as discarded"
    )
    assert isinstance(decoded, DecodedStore), "the compacted store decodes"
    assert decoded.records == (), "the row is gone before the next restart"


def test_an_older_row_appended_later_is_dropped_by_its_own_append(
    tmp_path: Path,
) -> None:
    """The age check follows the oldest row, not the first row in the file.

    A row observed before every row it follows would otherwise survive until
    the file's first row aged past the cutoff too.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    writer = outcome_store.StoreWriter(
        store, policy=history_policy(1 << 20), wall_clock=lambda: NOW
    )
    try:
        wait_for_salt(writer)
        wait_for_results(writer, operation="load")
        append_and_wait(writer, window_summary(observed_at=NOW))
        expired = window_summary(observed_at=NOW - dt.timedelta(days=40))
        append_and_wait(writer, expired)
        compacted = wait_for_results(writer, operation="compact", reason="expired")
        decoded = outcome_store_read.read_store(store)
    finally:
        writer.close(1.0)

    assert any(result.discarded == 1 for result in compacted), (
        "the expired row is counted as discarded"
    )
    assert isinstance(decoded, DecodedStore), "the compacted store decodes"
    assert [record.observed_at for record in decoded.records] == [NOW], (
        "only the fresh row survives"
    )


def test_an_idle_writer_expires_aged_rows(tmp_path: Path) -> None:
    """A writer that receives no appends still lets aged rows leave.

    The age bound must not wait for a later append or for shutdown: a store
    that goes quiet would otherwise keep expired rows for as long as the
    process lives.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    now = [NOW]

    def monotonic() -> float:
        """Report the simulated wall clock on the monotonic scale."""
        return now[0].timestamp()

    writer = outcome_store.StoreWriter(
        store,
        policy=history_policy(1 << 20),
        wall_clock=lambda: now[0],
        monotonic_clock=monotonic,
        idle_tick=0.01,
    )
    try:
        wait_for_salt(writer)
        wait_for_results(writer, operation="load")
        append_and_wait(writer, window_summary(observed_at=NOW))
        now[0] = NOW + dt.timedelta(days=40)
        compacted = wait_for_results(writer, operation="compact", reason="expired")
        decoded = outcome_store_read.read_store(store)
    finally:
        writer.close(1.0)

    assert any(result.discarded == 1 for result in compacted), (
        "the aged row is counted as discarded"
    )
    assert isinstance(decoded, DecodedStore), "the compacted store decodes"
    assert decoded.records == (), "no row outlives the age bound"


def test_failed_append_keeps_the_previous_store_readable(tmp_path: Path) -> None:
    """A refused append reports failure and preserves complete earlier rows."""
    path = tmp_path / "cache-outcomes.jsonl"
    original = window_summary()
    with running_writer(path, wall_clock=lambda: _NOW) as writer:
        append_and_wait(writer, original)
        with mock.patch.object(
            outcome_store_state, "append_line", side_effect=OSError("disk full")
        ):
            writer.append(window_summary(server_version="0.15.0"))
            results = wait_for_results(writer, operation="append", reason="save_failed")

    assert_failed_append(path, results, (original,))
