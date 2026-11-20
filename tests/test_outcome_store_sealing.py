"""Writer-owned sealing of closed cache-outcome history.

The supervisor writer is the only component allowed to rotate the active file.
These tests pin the contract the archiver relies on: a sealed source keeps its
rows, metadata describes exactly those rows, the counter never reuses a
sequence, an empty seal claims nothing, the backlog is reported once per
eight-entry step, and a failed seal is reported rather than hidden.
"""

import json
import typing as typ
from pathlib import Path
from unittest import mock

from outcome_store_helpers import (
    close_writer,
    salt_path,
    segment_writer,
    wait_for_results,
    wait_for_salt,
)
from outcome_test_helpers import STORE_EPOCH as NOW
from outcome_test_helpers import record_after
from segment_test_helpers import document, history_policy, write_private

from buildlimits import outcome_record_encode, outcome_store_sealing
from buildlimits.outcome_key import Salt
from buildlimits.outcome_record_format import DecodedStore
from buildlimits.outcome_segment_state import read_sequence
from buildlimits.outcome_segments import (
    SegmentLayout,
    sealed_meta_name,
    segment_name,
)
from buildlimits.outcome_store import StoreWriter
from buildlimits.outcome_store_io import read_store
from buildlimits.outcome_store_salt import file_salt

if typ.TYPE_CHECKING:
    import pytest

    from buildlimits.outcome_ports import StoreResult
    from buildlimits.outcome_retention import RetentionPolicy


def _layout(store: Path) -> SegmentLayout:
    """Return the layout beside one store.

    Returns
    -------
    SegmentLayout
        The store's segment layout.
    """
    return SegmentLayout.beside(store)


def _sealed_names(store: Path) -> list[str]:
    """List the sealed source names in sequence order.

    Returns
    -------
    list[str]
        The ``segment-*.jsonl`` names.
    """
    directory = _layout(store).sealed_dir
    if not directory.exists():
        return []
    return sorted(path.name for path in directory.glob("*.jsonl"))


def _metadata(store: Path, sequence: int) -> dict[str, object]:
    """Read one sealed segment's writer metadata.

    Returns
    -------
    dict[str, object]
        The metadata document.
    """
    path = _layout(store).sealed_dir / sealed_meta_name(sequence)
    return json.loads(path.read_text())


def _active(store: Path) -> DecodedStore:
    """Read the active file, failing the test when it does not decode.

    Returns
    -------
    DecodedStore
        The active header and rows.
    """
    decoded = read_store(store)
    assert isinstance(decoded, DecodedStore), "the active file decodes"
    return decoded


def test_a_seal_publishes_a_source_and_its_metadata(tmp_path: Path) -> None:
    """The source keeps the rows and the sidecar describes them."""
    store = tmp_path / "cache-outcomes.jsonl"
    writer = segment_writer(store, history_policy(), NOW)
    try:
        writer.append(record_after(0))
        writer.append(record_after(1))
        wait_for_results(writer, operation="seal", reason="none")
    finally:
        close_writer(writer)

    salt = file_salt(store.with_name("cache-outcomes.salt"))
    assert isinstance(salt, Salt), "the stored salt is readable"
    assert _sealed_names(store) == [segment_name(1), segment_name(2)], (
        "each closed row becomes one numbered source"
    )
    document = _metadata(store, 1)
    assert document["schema"] == 1, "the metadata schema is closed"
    assert document["sequence"] == 1, "the sequence is named"
    assert document["salt_id"] == salt.salt_id, "the record domain is preserved"
    assert document["record_count"] == 1, "the sealed row is counted"
    assert document["first_observed_at"] == "2026-11-19T00:00:00Z", (
        "the first observation is recorded"
    )
    assert document["last_observed_at"] == "2026-11-19T00:00:00Z", (
        "the last observation is recorded"
    )
    assert document["sealed_at"] == "2026-11-19T00:00:00Z", "the seal time is recorded"


def test_the_active_file_is_reset_to_its_header(tmp_path: Path) -> None:
    """Sealing moves every row out and leaves an appendable header behind."""
    store = tmp_path / "cache-outcomes.jsonl"
    writer = segment_writer(store, history_policy(), NOW)
    try:
        writer.append(record_after(0))
        writer.append(record_after(1))
        wait_for_results(writer, operation="seal", reason="none")
    finally:
        close_writer(writer)

    active = _active(store)
    assert active.records == (), "the active file holds no rows"
    assert active.header.salt_id, "the active file still has a header"


def test_the_writer_never_creates_an_archive(tmp_path: Path) -> None:
    """Compression is the archiver's job; the writer only seals plain rows."""
    store = tmp_path / "cache-outcomes.jsonl"
    writer = segment_writer(store, history_policy(), NOW)
    try:
        writer.append(record_after(0))
        writer.append(record_after(1))
        wait_for_results(writer, operation="seal", reason="none")
    finally:
        close_writer(writer)

    assert not _layout(store).archive_dir.exists(), "the writer owns no archive"


def test_an_empty_seal_claims_no_sequence(tmp_path: Path) -> None:
    """Rotating a header-only file must not create a permanent gap."""
    store = tmp_path / "cache-outcomes.jsonl"
    writer = segment_writer(store, history_policy(), NOW)
    try:
        writer.append(record_after(0))
        wait_for_results(writer, operation="append", reason="none")

        layout = _layout(store)
        assert _sealed_names(store) == [], "nothing was sealed"
        assert read_sequence(layout.sequence_file) is None, "no sequence was claimed"
        assert _active(store).records != (), "the row stayed in the active file"
    finally:
        close_writer(writer)


def test_the_counter_follows_the_highest_sealed_name(tmp_path: Path) -> None:
    """A sequence already on disk is never reused after a counter loss."""
    store = tmp_path / "cache-outcomes.jsonl"
    layout = _layout(store)
    layout.sealed_dir.mkdir(parents=True)
    (layout.sealed_dir / segment_name(5)).write_bytes(b"older source")
    layout.sequence_file.write_text("3\n", encoding="utf-8")

    writer = segment_writer(store, history_policy(), NOW)
    try:
        writer.append(record_after(0))
        writer.append(record_after(1))
        wait_for_results(writer, operation="seal", reason="none")
    finally:
        close_writer(writer)

    names = _sealed_names(store)
    assert segment_name(6) in names, "the next sequence follows the directory"
    assert segment_name(3) not in names, "the stale counter is not trusted"
    assert read_sequence(layout.sequence_file) == 8, (
        "the counter records one past the second new sequence"
    )


def test_a_fresh_store_keeps_its_first_row(tmp_path: Path) -> None:
    """Startup installs the header so the first append has a file to reach."""
    store = tmp_path / "cache-outcomes.jsonl"
    writer = segment_writer(store, history_policy(segment_bytes=10**9), NOW)
    try:
        writer.append(record_after(0))
        wait_for_results(writer, operation="append", reason="none")
    finally:
        close_writer(writer)

    assert [record.observed_at for record in _active(store).records] == [NOW], (
        "the first row is written rather than discarded"
    )


def test_metadata_covers_every_row_sealed_together(tmp_path: Path) -> None:
    """A seal that closes two rows reports the whole span, not the last row."""
    store = tmp_path / "cache-outcomes.jsonl"
    writer = segment_writer(store, history_policy(segment_bytes=10**9), NOW)
    try:
        writer.append(record_after(0))
    finally:
        close_writer(writer)
    line_bytes = len(outcome_record_encode.encode_line(record_after(0))) + 1
    # Two rows fit the watermark and the third crosses it, so the seal closes
    # exactly two rows and the metadata must describe the whole span.
    policy = history_policy(
        segment_bytes=store.stat().st_size + line_bytes + line_bytes // 2
    )

    writer = segment_writer(store, policy, NOW)
    try:
        for minute in (1, 2):
            writer.append(record_after(minute))
        wait_for_results(writer, operation="seal", reason="none")
    finally:
        close_writer(writer)

    document = _metadata(store, 1)
    assert document["record_count"] == 2, "both rows are counted together"
    assert document["first_observed_at"] == "2026-11-19T00:00:00Z", (
        "the earliest row is reported"
    )
    assert document["last_observed_at"] == "2026-11-19T00:01:00Z", (
        "the latest row is reported"
    )


def test_the_backlog_is_reported_at_eight_entries(tmp_path: Path) -> None:
    """Four sealed sources and their metadata are one eight-entry step."""
    store = tmp_path / "cache-outcomes.jsonl"
    writer = segment_writer(store, history_policy(), NOW)
    try:
        for minute in range(4):
            writer.append(record_after(minute))
        wait_for_results(writer, operation="seal", reason="none")
        assert not [
            result for result in writer.results() if result.reason == "archive_backlog"
        ], "six entries are below the step"

        writer.append(record_after(4))
        observed = wait_for_results(writer, operation="seal", reason="archive_backlog")
    finally:
        close_writer(writer)

    backlog = [result for result in observed if result.reason == "archive_backlog"]
    assert backlog, "the fourth seal crosses the step"
    assert backlog[0].records == 8, "the report counts directory entries"


def _load_previous_domain(
    store: Path, rows: int, policy: RetentionPolicy
) -> tuple[Salt, tuple[StoreResult, ...]]:
    """Write a store under one salt, activate another and load the change.

    Returns
    -------
    tuple[Salt, tuple[StoreResult, ...]]
        The salt the seeded rows were keyed under and the startup outcomes,
        including the ``salt_changed`` result this helper waits for.
    """
    previous = file_salt(salt_path(store))
    assert isinstance(previous, Salt), "the fixture gets its first salt"
    records = tuple(record_after(minute) for minute in range(rows))
    write_private(store, document(records, previous.salt_id))
    salt_path(store).write_bytes(b"n" * 32)

    writer = StoreWriter(store, policy=policy, wall_clock=lambda: NOW)
    try:
        wait_for_salt(writer)
        results = wait_for_results(writer, operation="load", reason="salt_changed")
    finally:
        close_writer(writer)
    return previous, results


def test_a_salt_change_seals_the_previous_domain(tmp_path: Path) -> None:
    """An active file below the segment bound is still sealed on a salt change.

    Without the seal the old rows would stay in the active file and the new
    domain's first append would produce a segment mixing two salts.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    previous, results = _load_previous_domain(store, 2, history_policy(1024 * 1024))

    assert any(result.records == 2 for result in results), (
        "the preserved rows are reported rather than counted as discarded"
    )
    assert not any(result.discarded for result in results), (
        "a segmented store loses no rows to a salt change"
    )
    active_salt = file_salt(salt_path(store))
    assert isinstance(active_salt, Salt), "the replacement salt is readable"
    assert _sealed_names(store) == [segment_name(1)], "the old rows are sealed"
    metadata = _metadata(store, 1)
    assert metadata["salt_id"] == previous.salt_id, (
        "the segment names the salt that keyed its rows"
    )
    assert metadata["record_count"] == 2, "every old row moved into the segment"
    active = _active(store)
    assert active.header.salt_id == active_salt.salt_id, "the header is the new salt"
    assert active.records == (), "no old row remains in the active file"


def test_an_oversized_salt_change_segment_keeps_its_own_salt(tmp_path: Path) -> None:
    """Startup sealing after the change still names the rows' own salt.

    The settle pass would seal this file anyway because it exceeds the segment
    bound, so only the metadata's domain separates the two paths.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    previous, _results = _load_previous_domain(store, 1, history_policy())

    assert _sealed_names(store) == [segment_name(1)], "one segment holds the rows"
    assert _metadata(store, 1)["salt_id"] == previous.salt_id, (
        "the sealed rows keep their own salt, not the newly activated one"
    )
    assert _active(store).records == (), "the active file starts the new domain"


def test_a_failed_seal_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seal that cannot write state reports a retryable save failure."""
    store = tmp_path / "cache-outcomes.jsonl"
    writer = segment_writer(store, history_policy(), NOW)
    failure = OSError("no space for the sequence counter")
    try:
        with mock.patch.object(
            outcome_store_sealing, "write_sequence", side_effect=failure
        ):
            writer.append(record_after(0))
            writer.append(record_after(1))
            wait_for_results(writer, operation="seal", reason="save_failed")
            assert _sealed_names(store) == [], "no partial source is published"
    finally:
        close_writer(writer)


def test_a_seal_that_cannot_sync_keeps_its_rows_closed(tmp_path: Path) -> None:
    """A sync fault after the rename does not resurrect the sealed rows."""
    store = tmp_path / "cache-outcomes.jsonl"
    writer = segment_writer(store, history_policy(), NOW)
    try:
        with mock.patch.object(
            outcome_store_sealing, "sync_rename", side_effect=OSError("sync failed")
        ):
            writer.append(record_after(0))
            writer.append(record_after(1))
            results = wait_for_results(writer, operation="seal", reason="save_failed")
    finally:
        close_writer(writer)

    assert any(
        result.operation == "seal"
        and result.reason == "save_failed"
        and result.records == 1
        for result in results
    ), "the failed sync is reported against the row it closed"
    assert _sealed_names(store) == [segment_name(1), segment_name(2)], (
        "the rename published the first source; the next seal publishes the second"
    )
    assert _metadata(store, 1)["record_count"] == 1, "metadata describes the source"
    assert _metadata(store, 2)["record_count"] == 1, (
        "the row sealed before the sync fault is never counted as active again"
    )
    assert _active(store).records == (), "the active file holds only its header"


def test_a_completed_rename_is_synced_though_no_fresh_file_is_installed(
    tmp_path: Path,
) -> None:
    """A seal that cannot install a fresh active file still syncs its rename.

    The rename has already moved the rows out of the active file. Skipping the
    directory sync because the replacement file failed would leave the source's
    name unsynced as well, so a crash could lose the only copy of the rows.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    writer = segment_writer(store, history_policy(), NOW)
    synced: list[tuple[Path, Path]] = []
    try:
        with (
            mock.patch.object(
                outcome_store_sealing, "restart_active", return_value=False
            ),
            mock.patch.object(
                outcome_store_sealing,
                "sync_rename",
                side_effect=lambda first, second: synced.append((first, second)),
            ),
        ):
            writer.append(record_after(0))
            writer.append(record_after(1))
            wait_for_results(writer, operation="seal", reason="save_failed")
    finally:
        close_writer(writer)

    layout = _layout(store)
    assert synced == [(layout.sealed_dir, store.parent)], (
        "the completed rename is synced in both directories"
    )
    assert _sealed_names(store) == [segment_name(1)], (
        "the source the rename published is still recorded"
    )


def test_a_seal_that_cannot_sync_the_source_keeps_it_active(tmp_path: Path) -> None:
    """A content sync failure leaves the rows in the active file to retry.

    Renaming a file does not make its unsynced contents durable, so the seal
    syncs the source first. A failure there must publish nothing: the source
    stays the active file and the next append retries the seal.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    writer = segment_writer(store, history_policy(), NOW)
    try:
        with mock.patch.object(
            outcome_store_sealing, "sync_file", side_effect=OSError("sync failed")
        ):
            writer.append(record_after(0))
            writer.append(record_after(1))
            wait_for_results(writer, operation="seal", reason="save_failed")
            # Read the store before the closing flush, which would otherwise
            # seal the rows this failure deliberately leaves active.
            assert _sealed_names(store) == [], (
                "no source is published with unsynced rows"
            )
            assert _active(store).records == (record_after(0), record_after(1)), (
                "the rows the failed seal left behind are still active"
            )
    finally:
        close_writer(writer)
