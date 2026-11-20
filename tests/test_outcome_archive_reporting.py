"""Reporting faults in an archive run without losing the work it did.

Every report a timer reads is assembled from what the run could measure. These
tests cover the cases where a measurement is missing: a corrupt active file, a
deferred run, a dry run and a read that fails after the archives were
published.
"""

import typing as typ
from pathlib import Path

import outcome_archive_helpers as archive

from buildlimits import outcome_segment_inventory
from buildlimits.outcome_archive import EXIT_CORRUPT, EXIT_FAILED
from buildlimits.outcome_archive_lock import ArchiveLock

if typ.TYPE_CHECKING:
    import pytest

    from buildlimits.outcome_segments import SegmentLayout


def test_a_corrupt_active_file_makes_the_retained_total_a_lower_bound(
    tmp_path: Path,
) -> None:
    """Closed history is still archived, but the run cannot claim success.

    The active file's size is measurable while its rows are not, so a timer
    that recorded ``ok`` would present an undercount as the exact retained
    history.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    archive.write(store, archive.records(archive.NOW, 2), archive.policy())
    damaged = b'{"format":"buildlimits-cache-outcomes"}\n'
    store.write_bytes(damaged)
    store.chmod(0o600)

    report = archive.run(tmp_path, archive.policy())

    assert len(archive.archives(tmp_path)) == 2, "readable closed history is archived"
    assert report.status == "corrupt", "the run names the active-file fault"
    assert report.exit_status == EXIT_CORRUPT, "an operator must look"
    assert report.active_bytes == len(damaged), "the file's size is still reported"
    assert report.retained is not None and not report.retained.complete, (
        "the retained totals are marked incomplete"
    )


def test_a_corrupt_active_file_fails_a_dry_run_without_writing_state(
    tmp_path: Path,
) -> None:
    """An inspection reports the same fault while changing nothing."""
    store = tmp_path / "cache-outcomes.jsonl"
    store.write_bytes(b"not a header\n")
    store.chmod(0o600)

    report = archive.run(tmp_path, archive.policy(), dry_run=True)

    assert report.dry_run, "the report still says no file changed"
    assert report.status == "corrupt", "inspection names the same fault"
    assert report.exit_status == EXIT_CORRUPT, "a caller can act on it"
    assert not report.maintenance.state_written, "a dry run installs no state"


def test_a_dry_run_reports_the_inventory_it_planned_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run must not rescan a directory another process may change.

    Re-reading after planning could describe a segment the plan never saw, so
    the reported history would not match the reported plan.
    """
    archive.seeded_store(tmp_path, archive.records(archive.NOW, 2))
    read_inventory = outcome_segment_inventory.read_inventory
    calls = 0

    def counted_read(
        inventory_layout: SegmentLayout, *, retention_floor: int | None = None
    ) -> outcome_segment_inventory.Inventory:
        """Count inventory reads without changing their result."""
        nonlocal calls
        calls += 1
        return read_inventory(inventory_layout, retention_floor=retention_floor)

    monkeypatch.setattr(outcome_segment_inventory, "read_inventory", counted_read)

    report = archive.run(tmp_path, archive.policy(), dry_run=True)

    assert calls == 1, "the dry run plans and reports from one inventory scan"
    assert report.retained is not None, "the report describes the planned history"
    assert report.retained.closed_segments == 2, "both planned segments are described"


def test_an_unreadable_store_fails_a_dry_run_without_writing_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed inspection keeps its dry-run flag and installs no state."""
    monkeypatch.setattr(outcome_segment_inventory, "read_inventory", archive.refuse)

    report = archive.run(tmp_path, archive.policy(), dry_run=True)

    assert report.status == "failed", "the run could not start"
    assert report.reason == "unreadable", "the reason is closed"
    assert report.dry_run, "the report keeps the caller's dry-run choice"
    assert not report.maintenance.state_written, "no state file was installed"


def test_a_busy_run_reports_that_it_wrote_no_state(tmp_path: Path) -> None:
    """A deferred run must not claim maintenance it never performed."""
    store = tmp_path / "cache-outcomes.jsonl"
    archive.write(store, archive.records(archive.NOW, 1), archive.policy())

    with ArchiveLock(archive.layout(tmp_path).lock_file):
        report = archive.run(tmp_path, archive.policy())

    assert report.status == "busy", "the run did not start"
    assert not report.maintenance.state_written, "a busy run installs no state"


def test_a_failed_post_work_read_keeps_the_work_already_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Archives published before a read failure must stay in the report.

    The retention pass and the later scans read the store this run has just
    published. Returning a pre-run failure would erase real published work
    from the record, so the report names the work and says only that it could
    not be measured.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    archive.write(store, archive.records(archive.NOW, 2), archive.policy())
    read_inventory = outcome_segment_inventory.read_inventory
    calls = 0

    def fail_after_planning(
        inventory_layout: SegmentLayout, *, retention_floor: int | None = None
    ) -> outcome_segment_inventory.Inventory:
        """Let the plan read succeed, then refuse every later scan."""
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("the store cannot be read")
        return read_inventory(inventory_layout, retention_floor=retention_floor)

    monkeypatch.setattr(
        outcome_segment_inventory, "read_inventory", fail_after_planning
    )

    report = archive.run(tmp_path, archive.policy())

    assert report.status == "failed", "the result could not be measured"
    assert report.exit_status == EXIT_FAILED, "a retry may recompute it"
    assert report.reason == "unreadable", "the reason is closed"
    assert [action.sequence for action in report.actions] == [1, 2], (
        "both published archives are still reported"
    )
    assert len(archive.archives(tmp_path)) == 2, "the archives are real on disk"
    store_layout = archive.layout(tmp_path)
    assert not store_layout.retention_file.exists(), "no floor is recorded"
    assert not store_layout.last_run_file.exists(), "no report is installed"
    assert not report.maintenance.state_written, "the report says so"
