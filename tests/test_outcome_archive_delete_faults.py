"""Deletion faults an archive run records and does not invert.

Eviction is oldest-first, so a deletion that fails must leave the entry, stop
the newer steps the budget planned, and publish nothing that would let a later
scan mistake the surviving sequence for history retention removed.
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
    refuse_removal_without_unlinking,
    run,
    segment_bytes_for,
    write,
)

from buildlimits import outcome_archive_files
from buildlimits.outcome_segment_inventory import read_inventory
from buildlimits.outcome_segment_state import read_retention_floor
from buildlimits.outcome_segments import removed_marker_name, segment_name

if typ.TYPE_CHECKING:
    import pytest

    from buildlimits.outcome_archive_context import ArchiveContext
    from buildlimits.outcome_segment_inventory import SegmentEntry


def _expired_store(root: Path, count: int, *, seeded_bound: int) -> None:
    """Write a store whose rows are older than the run's age bound.

    The writer drops a row that arrives already expired, so expired history is
    seeded under a longer bound than the run applies.

    Parameters
    ----------
    root : Path
        The directory the store lives under.
    count : int
        How many expired records to seed.
    seeded_bound : int
        The age bound the store is written under.
    """
    write(
        root / "cache-outcomes.jsonl",
        records(NOW - dt.timedelta(days=40), count),
        policy(max_age_days=seeded_bound),
    )


def test_a_failed_source_delete_is_recorded_and_keeps_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source that cannot be evicted is reported and left in place.

    History already outside the age bound is evicted without being compressed,
    so this run's only operation is the delete that fails here.
    """
    _expired_store(tmp_path, 1, seeded_bound=60)
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


def test_a_deletion_whose_unlink_failed_keeps_no_removal_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry that survives its failed deletion must not keep a marker.

    The marker is published before any unlink, so a deletion that stops at the
    first file would otherwise explain a later loss of a sequence that is
    still present.
    """
    _expired_store(tmp_path, 2, seeded_bound=60)
    # The entry is archived first, so the deletion under test is the whole
    # archive rather than a source the same run never published.
    run(tmp_path, policy(max_age_days=60))

    monkeypatch.setattr(
        outcome_archive_files,
        "remove_private_files",
        refuse_removal_without_unlinking,
    )

    report = run(tmp_path, policy(max_age_days=30))

    assert (1, "unreadable") in report.failures, "the failed deletion is reported"
    assert not (layout(tmp_path).archive_dir / removed_marker_name(1)).exists(), (
        "the surviving archive withdraws the marker"
    )
    assert archives(tmp_path), "the archive itself is untouched"


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
    # Seeded with no age bound, because the writer expires an aged row when it
    # compacts: a store written under the thirty-day bound would hold none.
    _expired_store(tmp_path, 1, seeded_bound=0)

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
