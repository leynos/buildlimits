"""Reporting faults in an archive run without losing the work it did.

Every report a timer reads is assembled from what the run could measure. These
tests cover the cases where a measurement is missing: a corrupt active file, a
deferred run, a dry run and a read that fails after the archives were
published.
"""

import dataclasses as dc
import json
import typing as typ
from pathlib import Path
from unittest import mock

import outcome_archive_helpers as archive

from buildlimits import (
    outcome_archive_fs,
    outcome_archive_retain,
    outcome_archive_run,
    outcome_segment_inventory,
)
from buildlimits.outcome_archive_exit import (
    EXIT_CORRUPT,
    EXIT_FAILED,
    EXIT_TEMPFAIL,
    exit_status,
)
from buildlimits.outcome_archive_lock import ArchiveLock

if typ.TYPE_CHECKING:
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
    assert report.reason == "version_mismatch", "the report names the closed fault"
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
    assert report.reason == "corrupt", "inspection names the closed fault"
    assert report.exit_status == EXIT_CORRUPT, "a caller can act on it"
    assert not report.maintenance.state_written, "a dry run installs no state"


def test_a_dry_run_reports_the_inventory_it_planned_from(tmp_path: Path) -> None:
    """A dry run reports the settled pair its plan was built from.

    The settled reader scans the directory either side of the active-file read,
    so a seal that lands mid-read cannot leave the plan counting neither copy;
    the accepted pair then feeds both the plan and the report. The two scans
    are the settling check, not a later report-time rescan.
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

    with mock.patch.object(outcome_segment_inventory, "read_inventory", counted_read):
        report = archive.run(tmp_path, archive.policy(), dry_run=True)

    assert calls == 2, "the settled reader confirms its inventory once"
    assert report.retained is not None, "the report describes the planned history"
    assert report.retained.closed_segments == 2, "both planned segments are described"
    assert report.retained.complete, "an undisturbed active file is still measured"
    assert report.reason is None, "a complete inspection reports no fault"


def test_an_unreadable_store_fails_a_dry_run_without_writing_state(
    tmp_path: Path,
) -> None:
    """A failed inspection keeps its dry-run flag and installs no state."""
    with mock.patch.object(outcome_segment_inventory, "read_inventory", archive.refuse):
        report = archive.run(tmp_path, archive.policy(max_bytes=424_242), dry_run=True)

    assert report.status == "failed", "the run could not start"
    assert report.reason == "unreadable", "the reason is closed"
    assert report.dry_run, "the report keeps the caller's dry-run choice"
    assert not report.maintenance.state_written, "no state file was installed"
    assert report.budget_bytes == 424_242, (
        "a start failure still reports the configured byte budget"
    )


def test_a_busy_run_reports_that_it_wrote_no_state(tmp_path: Path) -> None:
    """A deferred run must not claim maintenance it never performed."""
    store = tmp_path / "cache-outcomes.jsonl"
    archive.write(store, archive.records(archive.NOW, 1), archive.policy())

    with ArchiveLock(archive.layout(tmp_path).lock_file):
        report = archive.run(tmp_path, archive.policy(max_bytes=987_654))

    assert report.status == "busy", "the run did not start"
    assert not report.maintenance.state_written, "a busy run installs no state"
    assert report.budget_bytes == 987_654, (
        "a deferred run still reports the configured byte budget"
    )


def test_a_failed_post_work_read_keeps_the_work_already_done(
    tmp_path: Path,
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
    archive_dir = archive.layout(tmp_path).archive_dir

    def fail_after_archiving(
        inventory_layout: SegmentLayout, *, retention_floor: int | None = None
    ) -> outcome_segment_inventory.Inventory:
        """Let planning succeed, then refuse a scan once work is published."""
        if archive_dir.exists() and any(archive_dir.glob("*.jsonl.xz")):
            raise OSError("the store cannot be read")
        return read_inventory(inventory_layout, retention_floor=retention_floor)

    with mock.patch.object(
        outcome_segment_inventory, "read_inventory", fail_after_archiving
    ):
        report = archive.run(tmp_path, archive.policy())

    assert report.status == "failed", "the result could not be measured"
    assert exit_status(report.status) == EXIT_FAILED, "a retry may recompute it"
    assert report.reason == "unreadable", "the reason is closed"
    assert [action.sequence for action in report.actions] == [1, 2], (
        "both published archives are still reported"
    )
    assert len(archive.archives(tmp_path)) == 2, "the archives are real on disk"
    store_layout = archive.layout(tmp_path)
    assert not store_layout.retention_file.exists(), "no floor is recorded"
    assert not store_layout.last_run_file.exists(), "no report is installed"
    assert not report.maintenance.state_written, "the report says so"


def test_a_seal_during_the_run_is_not_counted_twice(tmp_path: Path) -> None:
    """Rows sealed mid-run are counted once, in the segment they moved into.

    The writer never takes the archive lock, so it can seal the file the plan
    read while the run works. Reporting that pre-run snapshot beside the final
    inventory would count the sealed rows in both places.
    """
    store = archive.unsealed_store(tmp_path)
    retain = outcome_archive_retain.retain

    def retain_then_seal(
        run: outcome_archive_run.ArchiveRun,
        outcome: outcome_archive_run.RunOutcome,
    ) -> bool:
        """Seal the active file just after the retention pass."""
        settled = retain(run, outcome)
        archive.seal_store(store)
        return settled

    with mock.patch.object(outcome_archive_retain, "retain", retain_then_seal):
        report = archive.run(tmp_path, archive.policy(max_bytes=0, segment_bytes=4000))

    retained = report.retained
    assert retained is not None, "the run measures what it retains"
    assert (retained.record_count, retained.complete) == (3, True), (
        "each row is counted once and both reads describe the same file"
    )


def test_a_writer_that_never_settles_reports_an_unsettled_run(
    tmp_path: Path,
) -> None:
    """A seal on every reading leaves no pair describing one store version.

    The settled reader scans the closed history either side of the active-file
    read, so one seal is detected and retried. A writer that seals across every
    attempt never yields a pair, and the run reports the store as unsettled
    rather than presenting a torn pair as an exact total.
    """
    store = archive.unsealed_store(tmp_path)
    read_inventory = outcome_segment_inventory.read_inventory

    def seal_then_read(
        inventory_layout: SegmentLayout, *, retention_floor: int | None = None
    ) -> outcome_segment_inventory.Inventory:
        """Grow and seal the active file as every settled scan begins."""
        if retention_floor is not None:
            archive.write(store, archive.records(archive.NOW, 1), archive.policy())
            archive.seal_store(store)
        return read_inventory(inventory_layout, retention_floor=retention_floor)

    with mock.patch.object(outcome_segment_inventory, "read_inventory", seal_then_read):
        report = archive.run(tmp_path, archive.policy(max_bytes=0, segment_bytes=4000))




def test_a_seal_during_planning_is_retried_to_one_exact_reading(
    tmp_path: Path,
) -> None:
    """A seal between the planning reads is settled, not reported as a gap.

    The settled reader scans the closed history either side of the active-file
    read. A writer that seals between the two is detected, and the plan and
    report are rebuilt from a fresh pair, so the rows are counted once in the
    segment they moved into rather than in neither observation.
    """
    store = archive.unsealed_store(tmp_path)
    read_inventory = outcome_segment_inventory.read_inventory
    sealed = False

    def seal_once_after_read(
        inventory_layout: SegmentLayout, *, retention_floor: int | None = None
    ) -> outcome_segment_inventory.Inventory:
        """Read the inventory, then seal once before the next read."""
        nonlocal sealed
        inventory = read_inventory(inventory_layout, retention_floor=retention_floor)
        if not sealed:
            sealed = True
            archive.seal_store(store)
        return inventory

    with mock.patch.object(
        outcome_segment_inventory, "read_inventory", seal_once_after_read
    ):
        report = archive.run(
            tmp_path,
            archive.policy(max_bytes=0, segment_bytes=4000),
            dry_run=True,
        )

    assert report.retained is not None, "the inspection still reports what it saw"
    assert report.retained.record_count == 3, "every row is counted once"
    assert report.retained.complete is True, (
        "the retried readings describe one version of the store"
    )


def test_a_failed_state_write_still_records_the_deletions_it_completed(
    tmp_path: Path,
) -> None:
    """A later run must inherit deletions whose run could not save its state.

    The retention pass has already removed the oldest archive when the state
    write fails. The floor is the only record that the sequence was deleted
    rather than lost, so the failed run gives it a second attempt. A later run
    with nothing left to delete then persists the advanced floor instead of
    reporting the deleted sequence as a gap for ever.
    """
    archive.seeded_store(tmp_path, archive.records(archive.NOW, 3))
    unlimited = archive.policy(max_bytes=0)
    archive.run(tmp_path, unlimited)
    files = archive.layout(tmp_path)
    write_json = outcome_archive_fs.atomic_write_json
    attempts = 0

    def fail_first_floor_write(target: Path, document: dict[str, object]) -> None:
        """Refuse the first floor write, then delegate every later one."""
        nonlocal attempts
        if target == files.retention_file:
            attempts += 1
            if attempts == 1:
                archive.refuse()
        write_json(target, document)

    with mock.patch.object(
        outcome_archive_fs, "atomic_write_json", fail_first_floor_write
    ):
        failed = archive.run(tmp_path, dc.replace(unlimited, max_records=2))

    assert failed.status == "failed", "the timer must retry the state write"
    assert [
        action.sequence for action in failed.actions if action.action == "deleted"
    ] == [1], "the run deleted the oldest archive before the write failed"

    recovered = archive.run(tmp_path, dc.replace(unlimited, max_records=2))
    floor = json.loads(files.retention_file.read_text(encoding="utf-8"))

    assert recovered.status == "ok", "the next firing completes normally"
    assert floor["floor_sequence"] == 2, "the floor advances past the deletion"
    assert not recovered.gaps, "the deletion is not reported as lost history"
