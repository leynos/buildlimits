"""Rollback and close faults in the cache-outcome store writer."""

import time
from pathlib import Path
from unittest import mock

import pytest
from outcome_store_fault_helpers import partial_append, stalled_append
from outcome_store_helpers import (
    append_and_wait,
    running_writer,
    stored_records,
    wait_for_results,
    worker_exited,
)
from outcome_test_helpers import STORE_EPOCH as NOW
from outcome_test_helpers import window_summary
from segment_test_helpers import write_private

from buildlimits import (
    outcome_store_io,
)
from buildlimits.outcome_ports import StoreResult


def test_failed_append_rollback_is_repaired_before_the_next_append(
    tmp_path: Path,
) -> None:
    """A failed truncate cannot turn the next complete row into corrupt data."""
    path = tmp_path / "cache-outcomes.jsonl"
    previous = window_summary(server_version="0.13.0")
    rejected = window_summary(server_version="0.14.0")
    appended = window_summary(server_version="0.15.0")

    with running_writer(path, wall_clock=lambda: NOW) as writer:
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

    The snapshot is read after the writer has stopped. The append result is
    published before the worker installs its snapshot, so a read inside the
    block could observe the state before the landed row.
    """
    path = tmp_path / "cache-outcomes.jsonl"
    original = window_summary()
    appended = window_summary(server_version="0.15.0")
    close = outcome_store_io.os.close
    fail_close = False

    with running_writer(path, wall_clock=lambda: NOW) as writer:
        append_and_wait(writer, original)
        store_identity = (path.stat().st_dev, path.stat().st_ino)

        def close_reporting_a_fault(descriptor: int) -> None:
            """Close one descriptor, faulting only for the store's own file.

            Patching ``os.close`` is process-wide, so the fault is narrowed by
            file identity: while the flag is set every unrelated close the
            process makes still succeeds.
            """
            try:
                status = outcome_store_io.os.fstat(descriptor)
            except OSError:
                status = None
            close(descriptor)
            is_store = (
                status is not None
                and (
                    status.st_dev,
                    status.st_ino,
                )
                == store_identity
            )
            if fail_close and is_store:
                raise OSError("close failed after the write")

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
    assert snapshot.records == 2, "the landed row is held in memory"
    assert snapshot.bytes == path.stat().st_size, (
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


def test_close_returns_within_its_bound_when_a_write_stalls(tmp_path: Path) -> None:
    """A stalled worker cannot extend the caller's one-second close bound."""
    path = tmp_path / "cache-outcomes.jsonl"
    with running_writer(path, wall_clock=lambda: NOW) as writer:
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
