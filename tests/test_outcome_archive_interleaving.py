"""A writer that seals while an archive run is in progress.

The writer never takes the archive lock, so it can close the active file into
a new source after the run has planned and before its follow-up passes look
again. These tests pin that the run archives what it discovers, applies the
budget to it in the same firing, and reports a retryable status instead of
``ok`` when new seals outpace its bounded follow-up work.
"""

import typing as typ
from pathlib import Path

from archive_cli_helpers import sealed_measured_source
from outcome_archive_helpers import (
    NOW,
    layout,
    policy,
    records,
    run,
    seal_active,
    seal_on_each_archive,
    write,
)
from segment_test_helpers import document, write_private

from buildlimits import outcome_archive_files
from buildlimits.outcome_segment_inventory import read_inventory

if typ.TYPE_CHECKING:
    import pytest

    from buildlimits.outcome_archive_context import ActionResult, ArchiveContext
    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.outcome_segment_inventory import SegmentEntry
    from buildlimits.outcome_segments import SegmentLayout


def _seal_after_first_archive(
    monkeypatch: pytest.MonkeyPatch,
    store_layout: SegmentLayout,
    sealed_rows: tuple[StoreRecord, ...],
    new_rows: tuple[StoreRecord, ...],
) -> None:
    """Seal the active rows once, after the run archives sequence one."""
    real = outcome_archive_files.archive_source

    def archive_then_seal(context: ArchiveContext, entry: SegmentEntry) -> ActionResult:
        """Archive one source, then seal the active rows once."""
        result = real(context, entry)
        if entry.sequence == 1:
            seal_active(store_layout, sealed_rows, 2)
            write_private(store_layout.active, document(new_rows))
        return result

    monkeypatch.setattr(outcome_archive_files, "archive_source", archive_then_seal)


def test_retention_settles_a_source_the_writer_seals_mid_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seal after the plan is archived and capped before the run reports ok."""
    store = tmp_path / "cache-outcomes.jsonl"
    store_layout = layout(tmp_path)
    active_rows = records(NOW, 4)
    write(store, active_rows, policy(segment_bytes=1_000_000))
    sealed_measured_source(store, 1, records(NOW, 1))
    _seal_after_first_archive(monkeypatch, store_layout, active_rows, records(NOW, 4))

    report = run(tmp_path, policy(max_age_days=0, max_records=5))

    actions = [(action.action, action.sequence) for action in report.actions]
    assert ("archived", 2) in actions, "the source sealed during the run is archived"
    assert ("trimmed", 2) in actions, (
        "the cap is applied once the new archive is measured"
    )
    assert report.retained_records == 5, "the final store holds the record cap"
    assert report.status == "ok", "a settled run succeeds"
    assert not read_inventory(store_layout).sealed, "no source is left behind"


def test_a_run_reports_unsettled_when_seals_outpace_its_follow_up_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seal on every pass leaves retryable work and a non-ok status."""
    store = tmp_path / "cache-outcomes.jsonl"
    store_layout = layout(tmp_path)
    sealed_measured_source(store, 1, records(NOW, 1))
    real = outcome_archive_files.archive_source
    seal_on_each_archive(monkeypatch, store, 5)

    report = run(tmp_path, policy(max_age_days=0))

    assert read_inventory(store_layout).sealed, (
        "the last seal waits for the next firing"
    )
    assert report.status == "unsettled", "a source arriving every pass is not settled"
    assert report.exit_status == 75, "the timer retries a run that could not settle"

    monkeypatch.setattr(outcome_archive_files, "archive_source", real)
    later = run(tmp_path, policy(max_age_days=0))

    assert later.status == "ok", "the next firing settles the store"
    assert not read_inventory(store_layout).sealed, "the last source is archived"
