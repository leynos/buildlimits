"""Listing a segment directory through the descriptor that was validated.

A scan is the inventory step a retention pass trusts before it deletes
anything. These tests replace the directory path immediately after validation,
so a scan that re-opened the path would report files from another directory.
"""

import typing as typ
from pathlib import Path

from segment_test_helpers import store_layout, swap_after_validation, write_private

from buildlimits import outcome_segment_scan

if typ.TYPE_CHECKING:
    import pytest


def test_a_scan_lists_the_validated_directory_after_its_path_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listing comes from the pinned descriptor, not a re-opened path."""
    archive_dir = store_layout(tmp_path).archive_dir
    archive_dir.mkdir(parents=True)
    kept = write_private(archive_dir / "segment-000000000001.jsonl.xz", b"real")
    moved = tmp_path / "archive-as-validated"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    write_private(elsewhere / "segment-000000000002.jsonl.xz", b"decoy")
    monkeypatch.setattr(
        outcome_segment_scan,
        "open_private_chain_when_present",
        swap_after_validation(
            outcome_segment_scan.open_private_chain_when_present,
            directory=archive_dir,
            moved=moved,
            elsewhere=elsewhere,
        ),
    )

    files = outcome_segment_scan.regular_files(archive_dir)

    assert [path.name for path, _size in files] == [kept.name], (
        "the scan reports the validated directory's file, not the replacement's"
    )
