"""The active file's expired rows do not decide what closed history survives.

The writer compacts rows older than the age cutoff on its next idle tick, so
they are not retained history. Both budgets must measure the active rows the
age rule keeps: counting the expired rows would evict closed history that
retention keeps, to make room for bytes and rows retention has already
expired. A complete run is what shows the difference, because the follow-up
passes could otherwise reintroduce the same mismeasurement after the first
plan.
"""

import datetime as dt
import errno
import typing as typ
from pathlib import Path

import pytest
from archive_cli_helpers import sealed_measured_source
from outcome_archive_helpers import NOW, archives, layout, policy, records, run, write
from segment_test_helpers import document, write_private

from buildlimits import outcome_archive_active, outcome_record_encode, outcome_store_io
from buildlimits.outcome_archive_report import EXIT_TEMPFAIL

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive_report import RunReport
    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_segments import SegmentLayout

_HEADER_BYTES = len(document(()))
_ARCHIVE_BYTES = len(document(records(NOW, 5)))
_EXPIRED = NOW - dt.timedelta(days=10)


def _archived_store(
    root: Path,
    *,
    retained: int = 5,
    active: tuple[StoreRecord, ...] = (),
) -> SegmentLayout:
    """Build a store with one published archive and the given active rows.

    The active file is written directly so the fixture can hold rows the
    writer's own compaction would have dropped. A real run sees exactly that
    state while it plans, because the writer may not have ticked yet.

    Returns
    -------
    SegmentLayout
        The layout of the store the run should use.
    """
    store = root / "cache-outcomes.jsonl"
    write(store, (), policy(segment_bytes=1_000_000))
    sealed_measured_source(store, 1, records(NOW, retained))
    first = run(root, policy(segment_bytes=1_000_000, max_age_days=30))
    assert first.status == "ok", "the fixture archive is published untrimmed"
    store_layout = layout(root)
    write_private(store_layout.active, document(active))
    return store_layout


def _run_over_expired_active(root: Path, run_policy: RetentionPolicy) -> RunReport:
    """Run one policy against an archive with expired active rows.

    The writer compacts the expired rows on its next idle tick, so they are not
    retained history. The archive is inside the age limit and must survive
    byte-identical under either budget; the caller then asserts what the report
    says about the same store.

    Parameters
    ----------
    root : Path
        The store root to build and run against.
    run_policy : RetentionPolicy
        The bounds to run with; its age rule must expire the active rows.

    Returns
    -------
    RunReport
        The report of the run.
    """
    _archived_store(root, active=records(_EXPIRED, 4))
    archive = archives(root)[0]
    before = archive.read_bytes()

    report = run(root, run_policy)

    assert report.status == "ok", "the retained history fits the budget"
    assert archive.read_bytes() == before, "the eligible archive is byte-identical"
    return report


def test_expired_active_bytes_do_not_evict_an_eligible_archive(
    tmp_path: Path,
) -> None:
    """A byte budget ignores the rows the writer is about to compact.

    The archived rows are inside the age limit and fit the budget. Only the
    expired rows in the active file push the physical total past it, so the
    archive must survive byte-identical rather than be rewritten for bytes
    retention has already released.
    """
    report = _run_over_expired_active(
        tmp_path,
        policy(
            segment_bytes=1_000_000,
            max_age_days=1,
            max_bytes=_HEADER_BYTES + _ARCHIVE_BYTES,
        ),
    )

    assert report.retained_bytes > report.budget_bytes, (
        "the report still describes the expired rows the writer has not compacted"
    )


def test_expired_active_rows_do_not_evict_under_a_record_cap(
    tmp_path: Path,
) -> None:
    """A record cap ignores expired rows while the report still counts them.

    The cap bounds retained history, so the archived rows meet it on their own.
    The report describes what the active file physically holds until the writer
    compacts it, which is why the reported count is larger than the cap while
    the run still succeeds.
    """
    report = _run_over_expired_active(
        tmp_path, policy(segment_bytes=1_000_000, max_age_days=1, max_records=5)
    )

    assert report.retained_records == 9, (
        "the report counts the expired rows the cap ignores"
    )
    evictions = [
        action.action for action in report.actions if action.action != "archived"
    ]
    assert not evictions, "no closed history is evicted for expired rows"


def test_active_rows_the_age_rule_keeps_still_count(tmp_path: Path) -> None:
    """The rows a cutoff keeps are retained history and are budgeted.

    Ignoring expired rows must not become ignoring the active file: rows at or
    after the cutoff share the budget with closed history, so an active file
    that fills it leaves no room for the archive.
    """
    _archived_store(tmp_path, retained=2, active=records(NOW, 4))
    active_bytes = len(document(records(NOW, 4)))

    report = run(
        tmp_path,
        policy(segment_bytes=1_000_000, max_age_days=1, max_bytes=active_bytes),
    )

    assert archives(tmp_path) == [], "the kept active rows leave no room to retain"
    assert report.retained_bytes == active_bytes, (
        "the surviving store is the active history the age rule keeps"
    )
    assert report.status == "ok", "the evicted store meets the byte budget"


def test_kept_active_rows_count_towards_a_record_cap(tmp_path: Path) -> None:
    """A cap the active rows meet on their own evicts the closed history.

    The cap is met by the rows the age rule keeps, so the older archive must be
    removed rather than left to exceed the bound the run reports.
    """
    _archived_store(tmp_path, retained=2, active=records(NOW, 4))

    report = run(
        tmp_path, policy(segment_bytes=1_000_000, max_age_days=1, max_records=4)
    )

    assert report.retained_records == 4, "only the kept active rows remain"
    assert archives(tmp_path) == [], "the archive is evicted whole"
    assert report.status == "ok", "the evicted store meets the record cap"


def test_an_append_during_planning_is_budgeted_from_its_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete row appended during planning decides the byte budget.

    The writer never takes the archive lock, so it can append between the size
    reading and the scan. A single scan would pair the earlier size with the
    later rows, count less history than the file holds, and plan no eviction
    for an archive the settled store cannot afford. An inspection reports that
    plan directly, so it shows the mismeasurement; the run that follows then
    checks the decision is applied and that the report describes the version
    its row count came from.
    """
    store_layout = _archived_store(tmp_path, retained=5, active=records(NOW, 2))
    budget = _ARCHIVE_BYTES + len(document(records(NOW, 2))) + 1
    real = outcome_archive_active._size_of
    appended = iter((records(NOW, 3)[2], records(NOW, 4)[3]))
    grew = False

    def grow_after_size(source: typ.BinaryIO, *, fallback: int = 0) -> int:
        """Append one complete row after the next size reading."""
        nonlocal grew
        size = real(source, fallback=fallback)
        if not grew:
            grew = True
            line = outcome_record_encode.encode_line(next(appended))
            with store_layout.active.open("ab") as handle:
                handle.write((line + "\n").encode())
        return size

    monkeypatch.setattr(outcome_archive_active, "_size_of", grow_after_size)
    run_policy = policy(segment_bytes=1_000_000, max_age_days=0, max_bytes=budget)

    inspection = run(tmp_path, run_policy, dry_run=True)

    assert inspection.active_bytes == len(document(records(NOW, 3))), (
        "the inspection measures the version its rows were read from"
    )
    assert inspection.plan is not None and inspection.plan.trim, (
        "the appended row is counted before the budget decides"
    )
    assert inspection.retained_bytes <= inspection.budget_bytes, (
        "the planned store meets the byte budget"
    )

    grew = False
    report = run(tmp_path, run_policy)

    assert report.active_bytes == len(document(records(NOW, 4))), (
        "the report measures the version its rows were read from"
    )
    evictions = [
        action for action in report.actions if action.action in {"trimmed", "deleted"}
    ]
    assert evictions, "the appended row decides the applied eviction"
    assert report.retained_bytes <= report.budget_bytes, (
        "the surviving store meets the byte budget"
    )
    assert report.status == "ok", "the settled store meets the byte budget"


def test_an_uncommitted_append_does_not_evict_an_eligible_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row whose newline is not written is not retained history.

    The writer commits an append by writing its newline and truncates a failed
    append back out. A scan that catches the row before its newline must not
    count it towards a budget: doing so evicts an archive the committed rows
    fit beside. This test pauses the append inside ``write_all``, runs the
    archiver against the half-written row, then fails the write so the writer's
    own rollback removes it.
    """
    store_layout = _archived_store(tmp_path, retained=2, active=records(NOW, 4))
    archive = archives(tmp_path)[0]
    before = archive.read_bytes()
    committed = document(records(NOW, 4))
    reports: list[RunReport] = []
    real_write_all = outcome_store_io.write_all
    in_window = False
    run_policy = policy(segment_bytes=1_000_000, max_age_days=1, max_records=6)

    def pause_before_newline(descriptor: int, data: bytes) -> None:
        """Write the row, let the run observe it, then fail its newline."""
        nonlocal in_window
        if in_window:
            real_write_all(descriptor, data)
            return
        in_window = True
        try:
            real_write_all(descriptor, data[:-1])
            reports.append(run(tmp_path, run_policy))
        finally:
            in_window = False
        raise OSError(errno.EIO, "injected newline failure")

    monkeypatch.setattr(outcome_store_io, "write_all", pause_before_newline)
    row = records(NOW, 5)[4]

    with pytest.raises(OSError, match="injected newline failure"):
        outcome_store_io.append_line(
            store_layout.active, outcome_record_encode.encode_line(row).encode()
        )

    assert len(reports) == 1, "the run observed the append before its newline"
    report = reports[0]
    assert report.retained_records == 6, "the uncommitted row is not counted"
    assert archive.read_bytes() == before, "the eligible archive is byte-identical"
    assert store_layout.active.read_bytes() == committed, (
        "the failed append is rolled back to the committed rows"
    )


def test_an_unmeasurable_active_file_defers_an_age_aware_byte_budget(
    tmp_path: Path,
) -> None:
    """A byte budget never evicts against a file whose survivors are unknown.

    With an age cutoff the budget counts only the active bytes retention
    keeps, and those cannot be read from an undecodable file. Evicting closed
    history against the whole file could discard an archive the settled store
    keeps, so the budget is deferred and the run reports that the timer should
    retry. A record cap configured beside it remains an operational failure,
    which the active-file tests cover.
    """
    store_layout = _archived_store(tmp_path, retained=5, active=records(_EXPIRED, 4))
    archive = archives(tmp_path)[0]
    before = archive.read_bytes()
    # Large enough that the whole file, read as retained bytes, would push the
    # archive past the budget and force a trim of history it cannot measure.
    write_private(store_layout.active, b"not a cache outcome document\n" * 100)

    report = run(
        tmp_path,
        policy(
            segment_bytes=1_000_000,
            max_age_days=1,
            max_bytes=_HEADER_BYTES + _ARCHIVE_BYTES,
        ),
    )

    assert archive.read_bytes() == before, "no eviction without a measurement"
    assert report.status == "unsettled", "the deferred budget is retryable"
    assert report.exit_status == EXIT_TEMPFAIL, "the timer retries the run"
    evictions = [action for action in report.actions if action.action != "archived"]
    assert not evictions, "nothing is evicted against an unread active file"
