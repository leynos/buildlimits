"""Writer-owned sealing of closed cache-outcome history.

The supervisor writer is the only component allowed to rotate the active file.
These tests pin the contract the archiver relies on: a sealed source keeps its
rows, metadata describes exactly those rows, the counter never reuses a
sequence, an empty seal claims nothing, the backlog is reported once per
eight-entry step, and a failed seal is reported rather than hidden.
"""

import datetime as dt
import json
import typing as typ
from pathlib import Path
from unittest import mock

from outcome_store_helpers import wait_for_results, wait_for_salt
from outcome_test_helpers import attributed_record

from buildlimits import outcome_record_encode, outcome_store_sealing
from buildlimits.outcome_key import Salt
from buildlimits.outcome_record_format import DecodedStore
from buildlimits.outcome_retention import RetentionPolicy
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

    from buildlimits.outcome_record_format import StoreRecord

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)


def _policy(segment_bytes: int = 1) -> RetentionPolicy:
    """Build an unlimited history policy that seals at ``segment_bytes``.

    Returns
    -------
    RetentionPolicy
        A policy with no age or byte eviction.
    """
    return RetentionPolicy(
        max_age=dt.timedelta(days=30),
        max_bytes=0,
        max_records=None,
        headroom_bytes=max(segment_bytes, 1) * 4,
        segment_bytes=segment_bytes,
    )


def _record(minute: int) -> StoreRecord:
    """Build one attributed record observed ``minute`` minutes after the epoch.

    Returns
    -------
    StoreRecord
        The record.
    """
    return attributed_record(observed_at=NOW + dt.timedelta(minutes=minute))


def _ready_writer(path: Path, policy: RetentionPolicy) -> StoreWriter:
    """Start a writer and wait until it accepts appends.

    Returns
    -------
    StoreWriter
        The initialized writer.
    """
    writer = StoreWriter(path, policy=policy, wall_clock=lambda: NOW)
    try:
        wait_for_salt(writer)
        wait_for_results(writer, operation="load")
    except BaseException:
        writer.close(1.0)
        raise
    return writer


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
    writer = _ready_writer(store, _policy())
    try:
        writer.append(_record(0))
        writer.append(_record(1))
        wait_for_results(writer, operation="seal", reason="none")
    finally:
        writer.close(2.0)

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
    writer = _ready_writer(store, _policy())
    try:
        writer.append(_record(0))
        writer.append(_record(1))
        wait_for_results(writer, operation="seal", reason="none")
    finally:
        writer.close(2.0)

    active = _active(store)
    assert active.records == (), "the active file holds no rows"
    assert active.header.salt_id, "the active file still has a header"


def test_the_writer_never_creates_an_archive(tmp_path: Path) -> None:
    """Compression is the archiver's job; the writer only seals plain rows."""
    store = tmp_path / "cache-outcomes.jsonl"
    writer = _ready_writer(store, _policy())
    try:
        writer.append(_record(0))
        writer.append(_record(1))
        wait_for_results(writer, operation="seal", reason="none")
    finally:
        writer.close(2.0)

    assert not _layout(store).archive_dir.exists(), "the writer owns no archive"


def test_an_empty_seal_claims_no_sequence(tmp_path: Path) -> None:
    """Rotating a header-only file must not create a permanent gap."""
    store = tmp_path / "cache-outcomes.jsonl"
    writer = _ready_writer(store, _policy())
    try:
        writer.append(_record(0))
        wait_for_results(writer, operation="append", reason="none")

        layout = _layout(store)
        assert _sealed_names(store) == [], "nothing was sealed"
        assert read_sequence(layout.sequence_file) is None, "no sequence was claimed"
        assert _active(store).records != (), "the row stayed in the active file"
    finally:
        writer.close(2.0)


def test_the_counter_follows_the_highest_sealed_name(tmp_path: Path) -> None:
    """A sequence already on disk is never reused after a counter loss."""
    store = tmp_path / "cache-outcomes.jsonl"
    layout = _layout(store)
    layout.sealed_dir.mkdir(parents=True)
    (layout.sealed_dir / segment_name(5)).write_bytes(b"older source")
    layout.sequence_file.write_text("3\n", encoding="utf-8")

    writer = _ready_writer(store, _policy())
    try:
        writer.append(_record(0))
        writer.append(_record(1))
        wait_for_results(writer, operation="seal", reason="none")
    finally:
        writer.close(2.0)

    names = _sealed_names(store)
    assert segment_name(6) in names, "the next sequence follows the directory"
    assert segment_name(3) not in names, "the stale counter is not trusted"
    assert read_sequence(layout.sequence_file) == 8, (
        "the counter records one past the second new sequence"
    )


def test_a_fresh_store_keeps_its_first_row(tmp_path: Path) -> None:
    """Startup installs the header so the first append has a file to reach."""
    store = tmp_path / "cache-outcomes.jsonl"
    writer = _ready_writer(store, _policy(segment_bytes=10**9))
    try:
        writer.append(_record(0))
        wait_for_results(writer, operation="append", reason="none")
    finally:
        writer.close(2.0)

    assert [record.observed_at for record in _active(store).records] == [NOW], (
        "the first row is written rather than discarded"
    )


def test_metadata_covers_every_row_sealed_together(tmp_path: Path) -> None:
    """A seal that closes two rows reports the whole span, not the last row."""
    store = tmp_path / "cache-outcomes.jsonl"
    writer = _ready_writer(store, _policy(segment_bytes=10**9))
    try:
        writer.append(_record(0))
    finally:
        writer.close(2.0)
    line_bytes = len(outcome_record_encode.encode_line(_record(0))) + 1
    # Two rows fit the watermark and the third crosses it, so the seal closes
    # exactly two rows and the metadata must describe the whole span.
    policy = _policy(segment_bytes=store.stat().st_size + line_bytes + line_bytes // 2)

    writer = _ready_writer(store, policy)
    try:
        for minute in (1, 2):
            writer.append(_record(minute))
        wait_for_results(writer, operation="seal", reason="none")
    finally:
        writer.close(2.0)

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
    writer = _ready_writer(store, _policy())
    try:
        for minute in range(4):
            writer.append(_record(minute))
        wait_for_results(writer, operation="seal", reason="none")
        assert not [
            result for result in writer.results() if result.reason == "archive_backlog"
        ], "six entries are below the step"

        writer.append(_record(4))
        observed = wait_for_results(writer, operation="seal", reason="archive_backlog")
    finally:
        writer.close(2.0)

    backlog = [result for result in observed if result.reason == "archive_backlog"]
    assert backlog, "the fourth seal crosses the step"
    assert backlog[0].records == 8, "the report counts directory entries"


def test_a_failed_seal_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seal that cannot write state reports a retryable save failure."""
    store = tmp_path / "cache-outcomes.jsonl"
    writer = _ready_writer(store, _policy())
    failure = OSError("no space for the sequence counter")
    try:
        with mock.patch.object(
            outcome_store_sealing, "write_sequence", side_effect=failure
        ):
            writer.append(_record(0))
            writer.append(_record(1))
            wait_for_results(writer, operation="seal", reason="save_failed")
            assert _sealed_names(store) == [], "no partial source is published"
    finally:
        writer.close(2.0)
