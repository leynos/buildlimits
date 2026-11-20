"""Record-cap sealing hands closed rows to the archiver.

A record cap counts the rows the writer still holds, and only a closed segment
can be archived and evicted. The cap therefore seals the active file before
the next row would take it past the bound. A seal that fails cannot be evicted
at all, so the writer refuses the row instead of holding history no run could
remove.
"""

import json
import typing as typ
from pathlib import Path

from outcome_archive_helpers import policy as archive_policy
from outcome_archive_helpers import run as run_archive
from outcome_store_helpers import (
    append_and_wait,
    close_writer,
    segment_writer,
    stored_records,
    wait_for_results,
)
from outcome_test_helpers import STORE_EPOCH as NOW
from outcome_test_helpers import record_after
from segment_test_helpers import history_policy

from buildlimits import outcome_store_state
from buildlimits.outcome_ports import StoreResult
from buildlimits.outcome_segments import SegmentLayout, sealed_meta_name

if typ.TYPE_CHECKING:
    import pytest


def test_a_record_cap_seals_the_active_file_in_segmented_mode(
    tmp_path: Path,
) -> None:
    """A record cap closes the active file so the archiver can evict its rows.

    The segment size is far larger than the cap, so only the cap can seal the
    file. Without that seal a small active file would hold more rows than the
    policy allows, and no archive pass could ever see them.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    writer = segment_writer(store, history_policy(10**9, max_records=2), NOW)
    try:
        for minute in range(3):
            writer.append(record_after(minute))
        wait_for_results(writer, operation="seal", reason="none")
    finally:
        close_writer(writer)

    sidecar = SegmentLayout.beside(store).sealed_dir / sealed_meta_name(1)
    assert json.loads(sidecar.read_text())["record_count"] == 2, (
        "the cap-sized file is sealed whole"
    )
    assert [record.observed_at for record in stored_records(store)] == [
        record_after(2).observed_at
    ], "the newest row stays active"


def test_a_failed_record_cap_seal_refuses_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seal that fails at the record cap refuses the append.

    A seal that fails before its rename leaves every row in the active file,
    and the archiver cannot evict an active file. Accepting the next row would
    hold history past the cap that no archive pass could remove, so the writer
    refuses it and the store stays within its bound.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    writer = segment_writer(store, history_policy(10**9, max_records=2), NOW)

    def refuse_seal(_state: object) -> bool:
        """Model a record-cap seal that failed before its rename."""
        return False

    try:
        for minute in range(2):
            append_and_wait(writer, record_after(minute))
        with monkeypatch.context() as failing:
            failing.setattr(outcome_store_state, "seal", refuse_seal)
            writer.append(record_after(2))
            refused = wait_for_results(writer, operation="append", reason="save_failed")
    finally:
        close_writer(writer)

    assert StoreResult("append", "save_failed", 0, 1) in refused, (
        "the row that would take the active file past the cap is refused"
    )
    assert stored_records(store) == (record_after(0), record_after(1)), (
        "the active file still holds exactly the rows the cap allows"
    )

    report = run_archive(tmp_path, archive_policy(10**9, max_records=2))

    assert report.status == "ok", "the store stays inside its bound"
    assert not report.actions, "the active rows leave nothing to evict"
    assert report.retained_records == 2, "history never exceeds the record cap"
