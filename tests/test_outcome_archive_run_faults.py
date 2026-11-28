"""Per-segment faults a full archive run records and survives.

Every planned operation runs behind one fault boundary: a closed fault
quarantines the artefact it names, any other filesystem failure is recorded as
``unreadable``, and the remaining plan still runs. These tests drive those
boundaries through a real run rather than a single operation, because the
per-operation suites cannot show that a failure leaves the rest of the plan
intact.
"""

import dataclasses as dc
import datetime as dt
import errno
import os
import typing as typ
from pathlib import Path

from outcome_archive_helpers import (
    NOW,
    archives,
    layout,
    policy,
    records,
    refuse,
    restore_source,
    run,
    segment_bytes_for,
    write,
)

from buildlimits import outcome_archive_encode, outcome_archive_files
from buildlimits.outcome_archive_verify import OutputFaultError, SegmentFaultError
from buildlimits.outcome_segment_inventory import read_inventory
from buildlimits.outcome_segment_state import read_retention_floor
from buildlimits.outcome_segments import segment_name

if typ.TYPE_CHECKING:
    import pytest

    from buildlimits.outcome_archive_context import ActionResult, ArchiveContext
    from buildlimits.outcome_segment_inventory import SegmentEntry


def test_a_duplicate_pair_is_settled_by_the_run(tmp_path: Path) -> None:
    """A source left beside its archive is reconciled in a normal run."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())
    run(tmp_path, policy())
    source = restore_source(layout(tmp_path), 1)

    report = run(tmp_path, policy())

    assert report.status == "ok", "settling a duplicate is not a failure"
    assert [(action.action, action.sequence) for action in report.actions] == [
        ("reconciled", 1)
    ], "the redundant source is settled instead of archived twice"
    assert not source.exists(), "only the verified archive remains"


def test_a_failed_reconcile_is_recorded_and_the_run_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pair the run cannot settle is reported while the next pair is settled."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 2), policy())
    run(tmp_path, policy())
    store_layout = layout(tmp_path)
    for sequence in (1, 2):
        restore_source(store_layout, sequence)
    from buildlimits import outcome_archive_reconcile

    real = outcome_archive_reconcile.reconcile_duplicate

    def refuse_first(
        context: ArchiveContext, sealed: SegmentEntry, archived: SegmentEntry
    ) -> ActionResult:
        """Fail only the first pair, as a closed fault in its output would."""
        if sealed.sequence == 1:
            raise OutputFaultError("truncated_xz")
        return real(context, sealed, archived)

    monkeypatch.setattr(outcome_archive_reconcile, "reconcile_duplicate", refuse_first)

    report = run(tmp_path, policy())

    assert (1, "truncated_xz") in report.failures, "the fault is recorded"
    assert [action.sequence for action in report.actions] == [2], (
        "the pair after the failure is still settled"
    )
    assert report.status == "corrupt", "the run names the fault it met"


def test_a_temporary_that_will_not_decode_is_not_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fault in the run's own output leaves the readable source for retry."""

    def refuse_scan(*_args: object, **_kwargs: object) -> object:
        """Fail the verification pass over the temporary archive."""
        raise SegmentFaultError("truncated_xz")

    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())
    monkeypatch.setattr(outcome_archive_encode, "read_archive_facts", refuse_scan)

    report = run(tmp_path, policy())

    assert report.failures == ((1, "truncated_xz"),), "the output fault is reported"
    assert layout(tmp_path).sealed_dir.joinpath(segment_name(1)).exists(), (
        "the source is not quarantined"
    )
    assert not layout(tmp_path).quarantine_dir.exists(), "nothing is quarantined"


def test_an_unreadable_source_is_recorded_as_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filesystem failure during archiving is reported, not raised."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())

    monkeypatch.setattr(outcome_archive_files, "archive_source", refuse)

    report = run(tmp_path, policy())

    assert report.failures == ((1, "unreadable"),), "the failure is reported"
    assert not archives(tmp_path), "nothing is published from a failed read"
    assert report.status == "corrupt", "the run names the fault it met"


def test_a_failed_source_delete_is_recorded_and_keeps_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source that cannot be evicted is reported and left in place.

    History already outside the age bound is evicted without being compressed,
    so this run's only operation is the delete that fails here.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    old = NOW - dt.timedelta(days=40)
    # The writer drops a row that arrives already expired, so expired history
    # on disk is sealed under a longer bound than the run applies.
    write(store, records(old, 1), policy(max_age_days=60))

    monkeypatch.setattr(outcome_archive_files, "delete_source", refuse)

    report = run(tmp_path, policy(max_age_days=30))

    assert report.failures == ((1, "unreadable"),), "the failure is recorded"
    assert not report.actions, "the delete did not report success"
    assert report.status == "corrupt", "the run names the fault it met"
    assert layout(tmp_path).sealed_dir.joinpath(segment_name(1)).exists(), (
        "the unremovable source stays"
    )


def test_a_failed_older_delete_stops_before_a_newer_trim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eviction stops oldest-first when an older entry cannot be removed.

    The record cap plans a whole deletion for the oldest entry and a trim for
    the next one. If the deletion fails, trimming the newer archive would
    discard rows from history that survives while the entry the budget asked
    for is still present.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    # Three records per segment, so the record cap deletes the oldest segment
    # whole and trims the next one. The bound is derived from the row the
    # fixture writes rather than pinned, so a wider row cannot repartition the
    # history this scenario depends on.
    base = policy(segment_bytes=segment_bytes_for(3))
    write(store, records(NOW, 12), base)
    before = {
        entry.sequence: entry.record_count
        for entry in read_inventory(layout(tmp_path)).sealed
    }
    monkeypatch.setattr(outcome_archive_files, "delete_source", refuse)

    report = run(tmp_path, dc.replace(base, max_records=8))

    assert set(report.failures) == {(1, "unreadable")}, (
        "only the older deletion is reported as failed"
    )
    assert not [action for action in report.actions if action.action == "trimmed"], (
        "the newer archive is not rewritten after the older deletion fails"
    )
    kept = read_inventory(layout(tmp_path)).archived
    assert kept[0].sequence == 2, "the newer archive survives"
    assert kept[0].record_count == before[2], (
        "the straddler still holds every row it was archived with"
    )


def test_a_fault_after_removal_still_records_the_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source removed before a later fault is a deletion, not lost history.

    The removal deletes the source before making the change durable, so a
    fault in the step after it leaves the sequence gone from the store with
    nothing in the report saying it was removed on purpose. The floor would
    then never advance over it. The failure is still reported, but the
    completed removal is recorded beside it.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    old = NOW - dt.timedelta(days=40)
    # The rows are seeded under a policy with no age bound, because the writer
    # expires an aged row when it compacts: a store written under the
    # thirty-day bound would already hold none of them.
    write(store, records(old, 1), policy(max_age_days=0))

    def remove_then_refuse(context: ArchiveContext, entry: SegmentEntry) -> None:
        """Remove the entry's file, then report the fault after it."""
        entry.path.unlink()
        refuse()

    monkeypatch.setattr(outcome_archive_files, "delete_source", remove_then_refuse)

    report = run(tmp_path, policy(max_age_days=30))

    assert (1, "unreadable") in report.failures, "the failed removal is reported"
    assert [(action.action, action.sequence) for action in report.actions] == [
        ("deleted", 1)
    ], "the removal the run completed is recorded beside the fault"
    assert read_retention_floor(layout(tmp_path)).sequence == 2, (
        "the floor advances over the sequence the run removed"
    )


def test_an_unreadable_lookup_is_not_a_completed_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lookup that failed for another reason proves nothing about the entry.

    ``Path.exists`` answers ``False`` for every ``OSError``, so an entry that
    is present but cannot be inspected reads as absent. Recording a deletion
    from that answer would advance the floor over a sequence still in the
    store. Only a confirmed absence may stand as evidence the removal
    completed; any other lookup failure leaves the failure reported and the
    floor where it was.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    old = NOW - dt.timedelta(days=40)
    # Seeded under a policy with no age bound: the writer expires an aged row
    # when it compacts, so the store must be written before that bound applies.
    write(store, records(old, 1), policy(max_age_days=0))
    source = layout(tmp_path).sealed_dir / segment_name(1)
    stat = os.stat
    is_hidden = False

    def refuse_to_inspect(
        path: os.PathLike[str] | str, *, follow_symlinks: bool = True
    ) -> os.stat_result:
        """Fail the entry's own lookup while the injected fault is in force.

        Every other path is inspected normally, so the denial stands in for an
        entry the process may no longer read rather than for a broken run.

        Returns
        -------
        os.stat_result
            The status of a path the fault does not hide.

        Raises
        ------
        PermissionError
            For the hidden entry only.
        """
        if is_hidden and os.fspath(path) == os.fspath(source):
            raise PermissionError(errno.EACCES, "the entry cannot be inspected")
        return stat(path, follow_symlinks=follow_symlinks)

    def refuse_and_hide(*_args: object, **_kwargs: object) -> typ.NoReturn:
        """Refuse the removal, then hide the entry from path lookups."""
        nonlocal is_hidden
        is_hidden = True
        refuse()

    monkeypatch.setattr(os, "stat", refuse_to_inspect)
    monkeypatch.setattr(outcome_archive_files, "delete_source", refuse_and_hide)

    report = run(tmp_path, policy(max_age_days=30))
    is_hidden = False

    assert (1, "unreadable") in report.failures, "the failed removal is reported"
    assert [action.action for action in report.actions] == [], (
        "a lookup that failed is not evidence the entry is gone"
    )
    assert read_retention_floor(layout(tmp_path)).sequence == 1, (
        "the floor does not advance over an entry the run still holds"
    )


def _deflate_after_the_work(monkeypatch: pytest.MonkeyPatch, errno_value: int) -> None:
    """Make the post-work measurement fail with the supplied filesystem error."""
    from buildlimits import outcome_archive_execute

    def refuse_report(*_args: object, **_kwargs: object) -> typ.NoReturn:
        """Fail the report measurement after the archives were published."""
        raise OSError(errno_value, "injected failure")

    monkeypatch.setattr(outcome_archive_execute, "_report", refuse_report)


def test_a_full_filesystem_after_the_work_stays_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full disk after archivalling reports no_headroom, not a failure.

    The archives and evictions are already published when the result cannot be
    measured. A full filesystem is temporary, so it keeps the retryable
    classification the earlier headroom check gives it.
    """
    write(tmp_path / "cache-outcomes.jsonl", records(NOW, 1), policy())
    _deflate_after_the_work(monkeypatch, errno.ENOSPC)

    report = run(tmp_path, policy())

    assert report.status == "no_headroom", "a full disk is retried by the timer"
    assert report.reason == "no_headroom", "the closed reason names the fault"
    assert [(action.action, action.sequence) for action in report.actions] == [
        ("archived", 1)
    ], "the work the run completed is still reported"


def test_another_filesystem_fault_after_the_work_is_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any other filesystem fault stays a failure for an operator to read."""
    write(tmp_path / "cache-outcomes.jsonl", records(NOW, 1), policy())
    _deflate_after_the_work(monkeypatch, errno.EIO)

    report = run(tmp_path, policy())

    assert report.status == "failed", "an unexplained fault is not retryable"
    assert report.reason == "unreadable", "the reason names the closed category"


def test_a_failed_reconcile_due_to_a_filesystem_fault_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-closed reconcile fault is recorded and the run continues."""
    from buildlimits import outcome_archive_reconcile

    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())
    run(tmp_path, policy())
    restore_source(layout(tmp_path), 1)

    monkeypatch.setattr(outcome_archive_reconcile, "reconcile_duplicate", refuse)

    report = run(tmp_path, policy())

    assert (1, "unreadable") in report.failures, "the fault is recorded"
    assert report.status == "corrupt", "the run names the fault it met"


def test_a_failed_marker_cleanup_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale marker that cannot be removed is a recorded failure."""
    from segment_test_helpers import write_private

    from buildlimits import outcome_archive_run

    marker = layout(tmp_path).sealed_dir / "segment-000000000004.jsonl.trimming"
    write_private(marker, b'{"schema":1,"sequence":4}\n')

    monkeypatch.setattr(outcome_archive_run, "end_sealed_replacement", refuse)

    report = run(tmp_path, policy())

    assert (4, "unreadable") in report.failures, "the fault is recorded"
    assert marker.exists(), "the marker is left for the next firing"
