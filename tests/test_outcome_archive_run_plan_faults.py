"""Plan-level faults one archive run records and survives."""

import dataclasses as dc
import datetime as dt
import errno
import typing as typ
from pathlib import Path

from outcome_archive_helpers import (
    NOW,
    archives,
    document,
    layout,
    policy,
    records,
    restore_source,
    run,
    write,
)
from segment_test_helpers import write_private

from buildlimits import (
    outcome_archive_encode,
    outcome_archive_files,
    outcome_archive_retain,
    outcome_archive_settled,
    outcome_records,
)
from buildlimits.outcome_archive_active import active_history
from buildlimits.outcome_archive_budget import UnknownActiveHistoryError
from buildlimits.outcome_archive_exit import EXIT_TEMPFAIL, exit_status
from buildlimits.outcome_segment_inventory import read_inventory
from buildlimits.outcome_segment_state import read_retention_floor
from buildlimits.outcome_segments import archive_name, segment_name

# The plans below pin exact sequences, so the segment bound must hold the same
# number of rows the original fixtures assumed. A store row grew when the
# format gained its version 2 evidence, so the bound is derived from the row
# size instead of a literal that silently changes how many rows fit.
_ROW_BYTES = len(outcome_records.encode_line(records(NOW, 1)[0]).encode("utf-8")) + 1
_SEGMENT_BYTES = 4 * _ROW_BYTES

if typ.TYPE_CHECKING:
    import pytest

    from buildlimits.outcome_archive_context import ArchiveContext
    from buildlimits.outcome_segment_inventory import SegmentEntry


def test_a_duplicate_pair_settles_before_the_cap_evicts_newer_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A budget never discards newer rows while an older pair is unsettled.

    The initial plan excluded the duplicated sequence from the record cap, so
    its eviction was planned over the newer archive alone. Re-planning from
    the reconciled inventory counts both archives, and the older one is then
    deleted first. When that deletion fails, the newer archive must still hold
    every row it was published with.
    """
    older_rows = records(NOW - dt.timedelta(hours=2), 10)
    newer_rows = records(NOW - dt.timedelta(hours=1), 10)
    sealed = layout(tmp_path).sealed_dir
    sealed.mkdir(parents=True)
    write_private(sealed / segment_name(1), document(older_rows))
    write_private(sealed / segment_name(2), document(newer_rows))
    publish = policy(1_000_000, max_age_days=3650)
    run(tmp_path, publish)
    assert len(archives(tmp_path)) == 2, "the fixture publishes two archives"
    store_layout = layout(tmp_path)
    source = restore_source(store_layout, 1)
    newer = store_layout.archive_dir / archive_name(2)
    before = newer.read_bytes()
    real = outcome_archive_files.delete_archive

    def refuse_older(context: ArchiveContext, entry: SegmentEntry) -> None:
        """Refuse only the older archive's deletion."""
        if entry.sequence == 1:
            raise OSError(errno.EIO, "injected failure")
        real(context, entry)

    monkeypatch.setattr(outcome_archive_files, "delete_archive", refuse_older)

    report = run(tmp_path, dc.replace(publish, max_records=5))

    assert report.status == "corrupt", "the failed older deletion needs an operator"
    assert report.failures == ((1, "unreadable"),), (
        "only the older deletion is reported as failed"
    )
    assert not source.exists(), "reconciliation settles the duplicate first"
    assert store_layout.archive_dir.joinpath(archive_name(1)).exists(), (
        "the older archive survives its failed deletion"
    )
    assert newer.read_bytes() == before, (
        "the newer archive is not rewritten while the older entry survives"
    )


def test_space_exhausted_after_the_headroom_check_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A disk that fills mid-pass defers the run and keeps the history.

    Headroom is checked once, before the pass, and is not reserved: another
    writer can consume the free space while this run works. A temporary write
    that meets ``ENOSPC`` after that check must leave the sealed source for
    the next timer firing and report the retryable status, not corruption.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 2), policy())
    store_layout = layout(tmp_path)

    def exhaust(
        _stream: object, sink: typ.BinaryIO, *, preset: int, trim: object
    ) -> typ.NoReturn:
        """Leave a partial archive behind and report a full filesystem."""
        sink.write(b"partial archive")
        raise OSError(errno.ENOSPC, "No space left on device")

    with monkeypatch.context() as failing:
        failing.setattr(outcome_archive_encode, "write_archive", exhaust)
        deflated = run(tmp_path, policy())

    assert deflated.status == "no_headroom", "a full disk is a retryable run"
    assert exit_status(deflated.status) == EXIT_TEMPFAIL, "the timer retries the run"
    assert deflated.failures == ((1, "no_headroom"), (2, "no_headroom")), (
        "each source the disk could not hold is named with the closed reason"
    )
    assert [
        store_layout.sealed_dir.joinpath(segment_name(sequence)).exists()
        for sequence in (1, 2)
    ] == [True, True], "no source is lost to a failed temporary"
    assert not archives(tmp_path), "nothing is published from a partial write"

    recovered = run(tmp_path, policy())

    assert recovered.status == "ok", "the next firing completes once space returns"
    assert [path.name for path in archives(tmp_path)] == [
        archive_name(1),
        archive_name(2),
    ], "both sources are archived by the retry"


def test_a_record_cap_evicts_the_oldest_source_in_the_planning_run(
    tmp_path: Path,
) -> None:
    """A cap is met by deleting the oldest source and trimming the straddler.

    The cap counts the rows the writer still holds in the active file, so the
    plan removes more from the closed history than the closed count alone
    suggests. The oldest source is outside the budget before it is ever
    compressed, so the plan deletes it whole rather than archiving history it
    would immediately remove; the trim is planned by the run's own plan, so
    this covers the run's trim step rather than the retention re-plan a
    heal-only run performs.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    base = policy(segment_bytes=_SEGMENT_BYTES)
    write(store, records(NOW, 12), base)
    active = active_history(layout(tmp_path).active)

    report = run(tmp_path, dc.replace(base, max_records=8))

    assert report.status == "ok", "a trim is not a failure"
    expected = [("archived", sequence) for sequence in (2, 3)]
    expected += [("deleted", 1), ("trimmed", 2)]
    assert [
        (action.action, action.sequence) for action in report.actions
    ] == expected, "the oldest source goes and the straddler is trimmed"
    kept = read_inventory(layout(tmp_path)).archived
    assert [entry.record_count for entry in kept] == [2, 3], (
        "the straddler keeps exactly the rows the cap allows"
    )
    assert active.records + sum(entry.record_count or 0 for entry in kept) == 8, (
        "the active rows count towards the cap"
    )


def test_a_symlinked_sealed_directory_is_refused_before_any_delete(
    tmp_path: Path,
) -> None:
    """A linked sealed directory never becomes a deletion outside the store.

    The inventory follows a directory argument, so a linked sealed directory
    would present an external expired segment as history. The run must refuse
    the store rather than remove the file the link names.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, (), policy(segment_bytes=1_000_000))
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / segment_name(1)
    write_private(external, b"external bytes")
    layout(tmp_path).sealed_dir.symlink_to(outside, target_is_directory=True)

    report = run(tmp_path, policy(max_age_days=0))

    assert report.status == "failed", "the unsafe store is not acted on"
    assert report.reason == "unreadable", "the operator-visible reason is closed"
    assert external.exists(), "the file outside the store is not deleted"


def test_an_unreadable_final_plan_records_the_completed_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deletion stays in the floor when the final plan cannot read the store.

    The pass re-plans after its work to confirm the budget is met. If that
    read cannot settle the record cap, the deletion the pass completed must
    still reach the floor before the unreadable report is returned; otherwise
    a later run reports the sequence as lost history.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    base = policy(segment_bytes=_SEGMENT_BYTES)
    write(store, records(NOW, 12), base)

    def refuse(*_args: object, **_kwargs: object) -> typ.NoReturn:
        """Report a cap the final read cannot settle."""
        raise UnknownActiveHistoryError

    monkeypatch.setattr(outcome_archive_settled, "plan_run", refuse)

    report = run(tmp_path, dc.replace(base, max_records=8))

    assert report.status == "failed", "an unreadable final plan needs an operator"
    assert report.reason == "unreadable", "the closed reason names the read"
    assert ("deleted", 1) in [
        (action.action, action.sequence) for action in report.actions
    ], "the completed deletion is reported"
    assert read_retention_floor(layout(tmp_path)).sequence == 2, (
        "the floor records the deleted sequence as intentional"
    )


def test_an_unreadable_retention_pass_records_the_completed_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retention replan failure still records what the pass deleted.

    ``retain`` re-plans from the store the run has just published. If that
    plan cannot read the active file after an eviction, the deletion floor
    must still learn what was removed, and the report must keep the actions.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    base = policy(segment_bytes=_SEGMENT_BYTES)
    write(store, records(NOW, 12), base)

    def refuse(*_args: object, **_kwargs: object) -> typ.NoReturn:
        """Report a cap the retention pass cannot settle."""
        raise UnknownActiveHistoryError

    monkeypatch.setattr(outcome_archive_retain, "retain", refuse)

    report = run(tmp_path, dc.replace(base, max_records=8))

    assert report.status == "failed", "an unreadable retention pass needs an operator"
    assert report.reason == "unreadable", "the closed reason names the read"
    assert ("deleted", 1) in [
        (action.action, action.sequence) for action in report.actions
    ], "the completed deletion is reported"
    assert read_retention_floor(layout(tmp_path)).sequence == 2, (
        "the floor records the deleted sequence as intentional"
    )
