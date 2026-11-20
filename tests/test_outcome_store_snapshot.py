"""One bounded read returns a store's size and its rows together."""

from pathlib import Path

from outcome_store_fixture_helpers import (
    append_torn_utf8_tail as _append_torn_utf8_tail,
)
from outcome_store_fixture_helpers import (
    seed_one_record_store as _seed_one_record_store,
)
from outcome_store_fixture_helpers import write_private_file as _write_private_file
from outcome_test_helpers import window_summary as _window_summary

from buildlimits import outcome_records, outcome_store_read


def test_snapshot_reports_bytes_and_rows_from_one_read(tmp_path: Path) -> None:
    """A reported size and row count describe the same read of the store."""
    path = tmp_path / "cache-outcomes.jsonl"
    record = _seed_one_record_store(path)
    _append_torn_utf8_tail(path, _window_summary(server_version="torn-é"))
    document = path.read_bytes()

    snapshot = outcome_store_read.read_store_snapshot(path)

    assert snapshot.logical_bytes == len(document), (
        "the byte count includes the header, its newline and the torn tail"
    )
    assert isinstance(snapshot.decoded, outcome_records.DecodedStore), (
        "the complete rows are still decoded"
    )
    assert snapshot.decoded.records == (record,), "the torn tail contributes no row"


def test_snapshot_reports_an_oversized_store_at_its_full_size(
    tmp_path: Path,
) -> None:
    """An unreadable store still has a size a diagnostic can report."""
    path = tmp_path / "cache-outcomes.jsonl"
    oversized = b"x" * (outcome_records.MAX_STORE_BYTES + 1)
    _write_private_file(path, oversized)

    snapshot = outcome_store_read.read_store_snapshot(path)

    assert snapshot.decoded == outcome_records.RecordFailure("corrupt"), (
        "bytes past the read bound are never decoded"
    )
    assert snapshot.logical_bytes == len(oversized), (
        "the report keeps the file's real size rather than the truncated read"
    )
