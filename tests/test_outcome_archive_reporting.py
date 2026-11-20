"""Reporting faults in an archive run without losing the work it did.

Every report a timer reads is assembled from what the run could measure. These
tests cover the cases where a measurement is missing: a corrupt active file, a
deferred run, a dry run and a read that fails after the archives were
published.
"""

import typing as typ
from pathlib import Path
from unittest import mock

import outcome_archive_helpers as archive

from buildlimits import outcome_archive_run, outcome_segment_inventory
from buildlimits.outcome_archive import EXIT_CORRUPT, EXIT_FAILED
from buildlimits.outcome_archive_lock import ArchiveLock

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive import RunReport
    from buildlimits.outcome_segments import SegmentLayout


def assert_replaced_history_is_a_lower_bound(report: RunReport, surface: str) -> None:
    """Require a report to refuse a complete history after a replacement.

    Parameters
    ----------
    report : RunReport
        The report assembled from reads a seal raced.
    surface : str
        The reporting surface's name, such as ``run`` or ``inspection``.
    """
    retained = report.retained
    assert retained is not None, f"the {surface} still reports what it saw"
    assert retained.record_count == 0, (
        f"neither read of the {surface} holds the sealed rows"
    )
    assert retained.complete is False, (
        f"rows the {surface} could not see are a lower bound"
    )
    assert report.status == "corrupt", f"a replaced active file fails the {surface}"
    assert report.exit_status == EXIT_CORRUPT, "the timer sees a failed measurement"


def seal_on_final_scan(store: Path, retention_floor: int | None) -> None:
    """Seal the active file only when the scan being patched is a later one.

    The planning read and the measurement that follows retention read the
    same store, and only the later scans carry a retention floor. Sealing the
    planning read would move history into a segment the plan never saw, so
    every patch seals only the scan whose floor says retention has run.
    """
    if retention_floor is not None:
        archive.seal_store(store)


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


def test_a_dry_run_reports_the_inventory_it_planned_from(tmp_path: Path) -> None:
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

    with mock.patch.object(outcome_segment_inventory, "read_inventory", counted_read):
        report = archive.run(tmp_path, archive.policy(), dry_run=True)

    assert calls == 1, "the dry run plans and reports from one inventory scan"
    assert report.retained is not None, "the report describes the planned history"
    assert report.retained.closed_segments == 2, "both planned segments are described"
    assert report.retained.complete, "an undisturbed active file is still measured"


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

    with mock.patch.object(
        outcome_segment_inventory, "read_inventory", fail_after_planning
    ):
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


def test_a_seal_during_the_run_is_not_counted_twice(tmp_path: Path) -> None:
    """Rows sealed mid-run are counted once, in the segment they moved into.

    The writer never takes the archive lock, so it can seal the file the plan
    read while the run works. Reporting that pre-run snapshot beside the final
    inventory would count the sealed rows in both places.
    """
    store = archive.unsealed_store(tmp_path)
    retain = outcome_archive_run.retain

    def retain_then_seal(
        run: outcome_archive_run.ArchiveRun,
        outcome: outcome_archive_run.RunOutcome,
    ) -> None:
        """Seal the active file just after the retention pass."""
        retain(run, outcome)
        archive.seal_store(store)

    with mock.patch.object(outcome_archive_run, "retain", retain_then_seal):
        report = archive.run(tmp_path, archive.policy(max_bytes=0, segment_bytes=4000))

    retained = report.retained
    assert retained is not None, "the run measures what it retains"
    assert (retained.record_count, retained.complete) == (3, True), (
        "each row is counted once and both reads describe the same file"
    )


def test_a_seal_during_measurement_makes_the_totals_a_lower_bound(
    tmp_path: Path,
) -> None:
    """A replacement inside the measurement window is reported as incomplete.

    The closed history is read first and the active file second. A seal
    between the two leaves them describing different files, so neither an
    exact count nor a silently undercounted one is acceptable.
    """
    store = archive.unsealed_store(tmp_path)
    read_inventory = outcome_segment_inventory.read_inventory

    def seal_then_read(
        inventory_layout: SegmentLayout, *, retention_floor: int | None = None
    ) -> outcome_segment_inventory.Inventory:
        """Seal the active file as the final scan begins."""
        seal_on_final_scan(store, retention_floor)
        return read_inventory(inventory_layout, retention_floor=retention_floor)

    with mock.patch.object(outcome_segment_inventory, "read_inventory", seal_then_read):
        report = archive.run(tmp_path, archive.policy(max_bytes=0, segment_bytes=4000))

    assert report.retained is not None, "the run still reports what it saw"
    assert (report.retained.record_count, report.retained.active_records) == (3, 0), (
        "the sealed rows are counted once and the replaced file contributes none"
    )
    assert report.retained.complete is False, (
        "an unmeasured active file makes the totals a lower bound"
    )


def test_a_seal_after_the_final_inventory_read_is_a_lower_bound(
    tmp_path: Path,
) -> None:
    """Rows that move after the last inventory read belong to neither read.

    The measurement reads closed history first and the active file second. A
    seal between those reads leaves the saved inventory without the new
    segment and an empty file at the active path, so the run must report a
    lower bound rather than a complete zero-row history.
    """
    store = archive.unsealed_store(tmp_path)
    read_inventory = outcome_segment_inventory.read_inventory

    def read_then_seal(
        inventory_layout: SegmentLayout, *, retention_floor: int | None = None
    ) -> outcome_segment_inventory.Inventory:
        """Return the inventory as it was before the final scan seals the store."""
        inventory = read_inventory(inventory_layout, retention_floor=retention_floor)
        seal_on_final_scan(store, retention_floor)
        return inventory

    with mock.patch.object(outcome_segment_inventory, "read_inventory", read_then_seal):
        report = archive.run(tmp_path, archive.policy(max_bytes=0, segment_bytes=4000))

    assert_replaced_history_is_a_lower_bound(report, "run")


def test_a_seal_during_planning_makes_a_dry_run_a_lower_bound(
    tmp_path: Path,
) -> None:
    """Inspection must not present a seal race as a complete zero-row history.

    A dry run plans from the inventory and the active file. If the writer
    seals between those reads, the rows live in a segment the inventory never
    saw and in neither observation, so a complete zero-row history would be a
    lie about what the store retains.
    """
    store = archive.unsealed_store(tmp_path)
    read_inventory = outcome_segment_inventory.read_inventory

    def seal_after_read(
        inventory_layout: SegmentLayout, *, retention_floor: int | None = None
    ) -> outcome_segment_inventory.Inventory:
        """Read the inventory, then seal before the active file is read."""
        inventory = read_inventory(inventory_layout, retention_floor=retention_floor)
        archive.seal_store(store)
        return inventory

    with mock.patch.object(
        outcome_segment_inventory, "read_inventory", seal_after_read
    ):
        report = archive.run(
            tmp_path,
            archive.policy(max_bytes=0, segment_bytes=4000),
            dry_run=True,
        )

    assert_replaced_history_is_a_lower_bound(report, "inspection")
