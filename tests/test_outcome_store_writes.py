"""Queue pressure, append faults and bounded shutdown for the store writer."""

import datetime as dt
import time
from pathlib import Path
from unittest import mock

import outcome_store_helpers as store_helpers
import pytest
from outcome_store_fault_helpers import partial_append, stalled_append
from outcome_store_helpers import (
    append_and_wait,
    assert_failed_append,
    running_writer,
    salt_path,
    start_writer,
    stored_records,
    wait_for_results,
    worker_exited,
)
from outcome_test_helpers import STORE_EPOCH as NOW
from outcome_test_helpers import window_summary
from segment_test_helpers import document, history_policy, write_private

from buildlimits import (
    outcome_records,
    outcome_store,
    outcome_store_io,
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
    with running_writer(path, queue_capacity=1) as writer:
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

    writer = outcome_store.StoreWriter(path, policy=history_policy(1 << 20))
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


def test_failed_append_keeps_the_previous_store_readable(tmp_path: Path) -> None:
    """A refused append reports failure and preserves complete earlier rows."""
    path = tmp_path / "cache-outcomes.jsonl"
    original = window_summary()
    with running_writer(path) as writer:
        append_and_wait(writer, original)
        with mock.patch.object(
            outcome_store_state, "append_line", side_effect=OSError("disk full")
        ):
            writer.append(window_summary(server_version="0.15.0"))
            results = wait_for_results(writer, operation="append", reason="save_failed")

    assert_failed_append(path, results, (original,))


def test_failed_append_rollback_is_repaired_before_the_next_append(
    tmp_path: Path,
) -> None:
    """A failed truncate cannot turn the next complete row into corrupt data."""
    path = tmp_path / "cache-outcomes.jsonl"
    previous = window_summary(server_version="0.13.0")
    rejected = window_summary(server_version="0.14.0")
    appended = window_summary(server_version="0.15.0")

    with running_writer(path) as writer:
        append_and_wait(writer, previous)
        with (
            partial_append(),
            mock.patch.object(
                outcome_store_io.os,
                "ftruncate",
                side_effect=OSError("truncate refused"),
            ),
        ):
            writer.append(rejected)
            failed = wait_for_results(writer, operation="append", reason="save_failed")

        assert StoreResult("append", "save_failed", 0, 1) in failed, (
            "a partial write is reported as one discarded row"
        )
        writer.append(appended)
        wait_for_results(writer, operation="append", reason="none")
        assert stored_records(path) == (previous, appended), (
            "the next append repairs the rejected partial row first"
        )


def test_a_close_error_after_a_complete_write_keeps_the_row(
    tmp_path: Path,
) -> None:
    """A row whose bytes reached disk is counted when only the close fails.

    The write finishes before the descriptor is closed, so a close error says
    nothing about the row. Treating it as a refused append would leave a row
    on disk that the writer's rows do not hold, and the next compaction would
    silently drop it.
    """
    path = tmp_path / "cache-outcomes.jsonl"
    original = window_summary()
    appended = window_summary(server_version="0.15.0")
    close = outcome_store_io.os.close
    fail_close = False

    def close_reporting_a_fault(descriptor: int) -> None:
        """Close the descriptor, then report a fault when the flag is set."""
        close(descriptor)
        if fail_close:
            raise OSError("close failed after the write")

    with running_writer(path) as writer:
        append_and_wait(writer, original)
        fail_close = True
        with mock.patch.object(outcome_store_io.os, "close", close_reporting_a_fault):
            writer.append(appended)
            results = wait_for_results(writer, operation="append", reason="none")
        fail_close = False
        snapshot = writer.retention()

    assert StoreResult("append", "none", 1, 0) in results, (
        "the row whose bytes reached disk is reported as appended"
    )
    assert stored_records(path) == (original, appended), "the row is readable on disk"
    assert snapshot.record_count == 2, "the landed row is held in memory"
    assert snapshot.logical_bytes == path.stat().st_size, (
        "the published count and size describe the same state"
    )


def test_a_refused_rollback_names_the_fragment_it_left(tmp_path: Path) -> None:
    """A rollback that cannot truncate reports bytes the caller must repair.

    The writer has to tell this apart from an ordinary refused append: the
    file's size no longer matches the rows it holds, so nothing may report the
    two as one state until a compaction rewrites the file.
    """
    path = write_private(tmp_path / "cache-outcomes.jsonl", b"")

    with (
        partial_append(),
        mock.patch.object(
            outcome_store_io.os, "ftruncate", side_effect=OSError("truncate refused")
        ),
        pytest.raises(outcome_store_io.TornTailError),
    ):
        outcome_store_io.append_line(path, b'{"format": "torn"}')

    assert path.stat().st_size > 0, "the fragment stayed on disk"


def test_a_failed_rollback_is_never_paired_with_the_row_count(
    tmp_path: Path,
) -> None:
    """The status snapshot reports a fragment as an unpaired measurement.

    The writer cannot remove a fragment its rollback could not truncate until
    its next compaction, so the status read must mark the size incomplete
    rather than present it beside a row count it does not describe.
    """
    path = tmp_path / "cache-outcomes.jsonl"
    salted = file_salt(salt_path(path))
    assert isinstance(salted, Salt), "the fixture installs its own salt"
    files = outcome_store_state.StoreFiles(
        path,
        mock.Mock(),
        Clocks(wall=lambda: NOW),
        policy=history_policy(1 << 20),
    )
    files.salt = salted
    files.load(salted, NOW)
    record = window_summary()
    line = outcome_records.encode_line(record).encode("utf-8")

    def tear(target: Path, _line: bytes) -> None:
        """Leave half a line behind, then report the failed rollback."""
        with target.open("ab") as handle:
            handle.write(b'{"format":')
        raise outcome_store_io.TornTailError("rollback refused")

    with mock.patch.object(outcome_store_state, "append_line", tear):
        files.append(line, record)
    files.publish_snapshot()
    unrepaired = files.retention()

    assert unrepaired.complete is False, "the fragment is not one measured state"
    assert unrepaired.reason == "repair_pending", "the reason names the repair"
    assert unrepaired.record_count == 0, "the refused row is not counted"
    assert unrepaired.logical_bytes == path.stat().st_size, (
        "the fragment is still part of the file's measured size"
    )

    assert files.compact() is True, "the next compaction repairs the file"
    files.publish_snapshot()
    repaired = files.retention()

    assert repaired.complete is True, "the repaired file is one state again"
    assert repaired.record_count == 0, "the refused row was never retained"
    assert repaired.logical_bytes == path.stat().st_size, "the size is the new file's"
    assert repaired.logical_bytes < unrepaired.logical_bytes, "the fragment is gone"


def test_close_returns_within_its_bound_when_a_write_stalls(tmp_path: Path) -> None:
    """A stalled worker cannot extend the caller's one-second close bound."""
    path = tmp_path / "cache-outcomes.jsonl"
    with running_writer(path) as writer:
        with stalled_append() as (entered, release):
            writer.append(window_summary())
            assert entered.wait(1.0), "the worker entered its stalled write"
            started = time.monotonic()
            writer.close(1.0)
            elapsed = time.monotonic() - started
            # The bound is meant to expire here, which leaves the worker inside
            # the write the stall is holding. Release it and wait for the worker
            # to finish, so the append and compaction it still runs happen
            # inside the test that caused them rather than after it. The wait is
            # far longer than the write needs because the point is to tolerate a
            # worker that waits for a timeslice on a heavily loaded host; pytest
            # bounds the test itself, so a genuinely wedged worker still fails.
            release.set()
            assert worker_exited(writer, 10.0), (
                "the worker stops once the write resumes"
            )

    assert elapsed < 1.25, "close honours the supplied one-second bound"
