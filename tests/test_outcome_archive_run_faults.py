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
import lzma
import typing as typ
from pathlib import Path

from outcome_archive_helpers import (
    NOW,
    archives,
    document,
    layout,
    policy,
    records,
    refuse,
    run,
    write,
)
from segment_test_helpers import write_private

from buildlimits import outcome_archive_encode, outcome_archive_files
from buildlimits.outcome_archive import EXIT_TEMPFAIL
from buildlimits.outcome_archive_active import active_history
from buildlimits.outcome_archive_verify import OutputFaultError, SegmentFaultError
from buildlimits.outcome_segment_inventory import read_inventory
from buildlimits.outcome_segments import archive_name, segment_name

if typ.TYPE_CHECKING:
    import pytest

    from buildlimits.outcome_archive_context import ActionResult, ArchiveContext
    from buildlimits.outcome_segment_inventory import SegmentEntry
    from buildlimits.outcome_segments import SegmentLayout


def _restore_source(store_layout: SegmentLayout, sequence: int) -> Path:
    """Rebuild one sealed source from the archive that replaced it.

    Restoring the source models the crash window a reconciling run settles: an
    archive published while its sealed source still exists.

    Parameters
    ----------
    store_layout : SegmentLayout
        The layout of the store the archive belongs to.
    sequence : int
        The archive sequence to restore.

    Returns
    -------
    Path
        The rebuilt sealed source.
    """
    archive = store_layout.archive_dir / archive_name(sequence)
    source = store_layout.sealed_dir / segment_name(sequence)
    source.write_bytes(lzma.decompress(archive.read_bytes()))
    source.chmod(0o600)
    return source


def test_a_duplicate_pair_is_settled_by_the_run(tmp_path: Path) -> None:
    """A source left beside its archive is reconciled in a normal run."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())
    run(tmp_path, policy())
    source = _restore_source(layout(tmp_path), 1)

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
        _restore_source(store_layout, sequence)
    real = outcome_archive_files.reconcile_duplicate

    def refuse_first(
        context: ArchiveContext, sealed: SegmentEntry, archived: SegmentEntry
    ) -> ActionResult:
        """Fail only the first pair, as a closed fault in its output would."""
        if sealed.sequence == 1:
            raise OutputFaultError("truncated_xz")
        return real(context, sealed, archived)

    monkeypatch.setattr(outcome_archive_files, "reconcile_duplicate", refuse_first)

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
    base = policy(segment_bytes=2000)
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
    source = _restore_source(store_layout, 1)
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
    assert deflated.exit_status == EXIT_TEMPFAIL, "the timer retries the run"
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
    base = policy(segment_bytes=2000)
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
