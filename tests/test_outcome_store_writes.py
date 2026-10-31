"""Queue pressure, append faults and bounded shutdown for the store writer."""

import datetime as dt
import time
from pathlib import Path
from unittest import mock

import outcome_store_helpers as store_helpers
import pytest
from outcome_store_helpers import (
    append_and_wait,
    assert_failed_append,
    running_writer,
    stalled_append,
    start_writer,
    stored_records,
    wait_for_results,
    window_summary,
)

from buildlimits import outcome_store, outcome_store_io
from buildlimits.outcome_ports import StoreResult


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


def test_failed_append_keeps_the_previous_store_readable(tmp_path: Path) -> None:
    """A refused append reports failure and preserves complete earlier rows."""
    path = tmp_path / "cache-outcomes.jsonl"
    original = window_summary()
    with running_writer(path) as writer:
        append_and_wait(writer, original)
        with mock.patch.object(
            outcome_store, "_append_line", side_effect=OSError("disk full")
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
        write = outcome_store_io.os.write

        def write_part_then_fail(descriptor: int, data: bytes) -> int:
            """Leave half a record on disk, then fail its append."""
            write(descriptor, data[: len(data) // 2])
            raise OSError("disk full")

        with (
            mock.patch.object(
                outcome_store_io.os, "write", side_effect=write_part_then_fail
            ),
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


def test_close_returns_within_its_bound_when_a_write_stalls(tmp_path: Path) -> None:
    """A stalled worker cannot extend the caller's one-second close bound."""
    path = tmp_path / "cache-outcomes.jsonl"
    with running_writer(path) as writer:
        with stalled_append() as (entered, _release):
            writer.append(window_summary())
            assert entered.wait(1.0), "the worker entered its stalled write"
            started = time.monotonic()
            writer.close(1.0)
            elapsed = time.monotonic() - started

    assert elapsed < 1.25, "close honours the supplied one-second bound"
