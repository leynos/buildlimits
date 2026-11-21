"""Sealing stays consistent through filesystem and counter faults.

A seal renames the writer's rows out of the active file and publishes the
metadata that describes them. These tests pin what happens when the steps
around that rename fail: a sync fault must not resurrect the rows or lose the
source name, and a damaged or exhausted counter must never publish a name the
inventory cannot see.
"""

from pathlib import Path
from unittest import mock

import pytest
from outcome_sealing_helpers import (
    active_of,
    layout_of,
    metadata_of,
    open_writer,
    seal_two_rows,
    sealed_names,
)
from outcome_test_helpers import record_after

from buildlimits import outcome_store_sealing
from buildlimits.outcome_segments import MAX_SEQUENCE, segment_name


def test_a_seal_that_cannot_sync_keeps_its_rows_closed(tmp_path: Path) -> None:
    """A sync fault after the rename does not resurrect the sealed rows."""
    store = tmp_path / "cache-outcomes.jsonl"
    with (
        mock.patch.object(
            outcome_store_sealing, "sync_rename", side_effect=OSError("sync failed")
        ),
        open_writer(store) as writer,
    ):
        results = seal_two_rows(writer, reason="save_failed")

    assert any(
        result.operation == "seal"
        and result.reason == "save_failed"
        and result.records == 1
        for result in results
    ), "the failed sync is reported against the row it closed"
    assert sealed_names(store) == [segment_name(1), segment_name(2)], (
        "the rename published the first source; the next seal publishes the second"
    )
    assert metadata_of(store, 1)["record_count"] == 1, "metadata describes the source"
    assert metadata_of(store, 2)["record_count"] == 1, (
        "the row sealed before the sync fault is never counted as active again"
    )
    assert active_of(store).records == (), "the active file holds only its header"


def test_a_completed_rename_is_synced_though_no_fresh_file_is_installed(
    tmp_path: Path,
) -> None:
    """A seal that cannot install a fresh active file still syncs its rename.

    The rename has already moved the rows out of the active file. Skipping the
    directory sync because the replacement file failed would leave the source's
    name unsynced as well, so a crash could lose the only copy of the rows.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    synced: list[tuple[Path, Path]] = []
    with (
        mock.patch.object(outcome_store_sealing, "restart_active", return_value=False),
        mock.patch.object(
            outcome_store_sealing,
            "sync_rename",
            side_effect=lambda first, second: synced.append((first, second)),
        ),
        open_writer(store) as writer,
    ):
        seal_two_rows(writer, reason="save_failed")

    layout = layout_of(store)
    assert synced == [(layout.sealed_dir, store.parent)], (
        "the completed rename is synced in both directories"
    )
    assert sealed_names(store) == [segment_name(1)], (
        "the source the rename published is still recorded"
    )


def test_a_seal_that_cannot_sync_the_source_keeps_it_active(tmp_path: Path) -> None:
    """A content sync failure leaves the rows in the active file to retry.

    Renaming a file does not make its unsynced contents durable, so the seal
    syncs the source first. A failure there must publish nothing: the source
    stays the active file and the next append retries the seal.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    with (
        mock.patch.object(
            outcome_store_sealing, "sync_file", side_effect=OSError("sync failed")
        ),
        open_writer(store) as writer,
    ):
        seal_two_rows(writer, reason="save_failed")
        # Read the store before the closing flush, which would otherwise seal
        # the rows this failure deliberately leaves active.
        assert sealed_names(store) == [], "no source is published with unsynced rows"
        assert active_of(store).records == (record_after(0), record_after(1)), (
            "the rows the failed seal left behind are still active"
        )


def test_an_out_of_range_counter_falls_back_to_the_directory(
    tmp_path: Path,
) -> None:
    """A damaged counter never forces an unsupported segment name.

    The twelve-digit inventory cannot see a longer name, so a counter beyond
    that range is ignored and the highest name on disk decides the next
    sequence instead.
    """
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    (sealed / segment_name(5)).write_bytes(b"older source")
    counter = tmp_path / "cache-outcomes.seq"
    counter.write_text(f"{MAX_SEQUENCE + 2}\n", encoding="utf-8")

    sequence = outcome_store_sealing.next_sequence(counter, sealed, tmp_path)

    assert sequence == 6, "the directory decides when the counter is damaged"


def test_an_exhausted_counter_refuses_the_next_sequence(tmp_path: Path) -> None:
    """Exhaustion survives retention removing the last nameable segment.

    The writer records the exhausted value in the counter when it seals the
    last nameable sequence. Retention may then delete that segment, so the
    directory names no longer prove the range is used. Reading the counter as
    damage would fall back to those names and reuse a lower sequence, so the
    exhausted value is a readable state rather than a fault.
    """
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    counter = tmp_path / "cache-outcomes.seq"
    counter.write_text(f"{MAX_SEQUENCE + 1}\n", encoding="utf-8")

    with pytest.raises(OverflowError, match="exhausted"):
        outcome_store_sealing.next_sequence(counter, sealed, tmp_path)


def test_the_next_sequence_refuses_an_exhausted_range(tmp_path: Path) -> None:
    """No seal may publish a name the inventory cannot see.

    The last nameable sequence is already taken, so the seal reports a save
    failure instead of writing a thirteen-digit file.
    """
    sealed = tmp_path / "sealed"
    sealed.mkdir()
    (sealed / segment_name(MAX_SEQUENCE)).write_bytes(b"last source")

    with pytest.raises(OverflowError, match="exhausted"):
        outcome_store_sealing.next_sequence(
            tmp_path / "cache-outcomes.seq", sealed, tmp_path
        )
