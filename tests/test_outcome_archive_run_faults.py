"""Per-segment faults a full archive run records and survives.

Every planned operation runs behind one fault boundary: a closed fault
quarantines the artefact it names, any other filesystem failure is recorded as
``unreadable``, and the remaining plan still runs. These tests drive those
boundaries through a real run rather than a single operation, because the
per-operation suites cannot show that a failure leaves the rest of the plan
intact.
"""

import errno
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
    write,
)

from buildlimits import outcome_archive_encode, outcome_archive_files
from buildlimits.outcome_archive_verify import OutputFaultError, SegmentFaultError
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
