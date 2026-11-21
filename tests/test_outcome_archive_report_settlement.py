"""The final report settles the store version it describes.

An executed run reads the store twice: once to plan it, and once after the
last retention pass to report it. The writer never takes the archive lock, so
it can append or seal between those readings. These tests pin the report's
answer to that: it re-reads until it describes one version, counts the rows
the writer moved, and reports retention it still owes rather than claiming
success over a store the next firing must evict.
"""

import datetime as dt
import itertools
import typing as typ
from pathlib import Path

from archive_seal_helpers import seal_five_rows, seal_one_row
from outcome_archive_helpers import (
    NOW,
    policy,
    records,
    run,
    store_with_archive,
)
from segment_test_helpers import document, write_private

from buildlimits import outcome_archive_retain, outcome_record_encode
from buildlimits.outcome_archive_exit import EXIT_TEMPFAIL, exit_status

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest

    from buildlimits.outcome_archive import RunReport
    from buildlimits.outcome_archive_history import ActiveHistory
    from buildlimits.outcome_segment_inventory import Inventory
    from buildlimits.outcome_segments import SegmentLayout


def _at_report_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    before_read: cabc.Callable[[], None],
) -> None:
    """Call ``before_read`` at each active-file read of the final report.

    An executed run reads the store through ``settled_snapshot`` exactly
    twice: once to plan it and once for the report. The second call is the
    report's, so a writer change injected inside that window lands after the
    last retention pass and before the report measures the store, however many
    attempts the report needs to settle.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        The fixture that replaces the snapshot and active-file seams.
    before_read : Callable[[], None]
        The writer change to inject before every report active-file read.
    """
    real_snapshot = outcome_archive_retain.settled_snapshot
    real_active = outcome_archive_retain.active_history
    snapshots = 0
    armed = False

    def snapshot_at_report(
        store_layout: SegmentLayout,
        floor: int | None,
        *,
        cutoff: dt.datetime | None = None,
    ) -> tuple[Inventory, ActiveHistory, bool]:
        """Arm the injection for the report's snapshot only."""
        nonlocal snapshots, armed
        snapshots += 1
        armed = snapshots == 2
        try:
            return real_snapshot(store_layout, floor, cutoff=cutoff)
        finally:
            armed = False

    def active_at_report(
        path: Path, *, cutoff: dt.datetime | None = None
    ) -> ActiveHistory:
        """Inject the writer change just before the report reads the file."""
        if armed:
            before_read()
        return real_active(path, cutoff=cutoff)

    monkeypatch.setattr(outcome_archive_retain, "settled_snapshot", snapshot_at_report)
    monkeypatch.setattr(outcome_archive_retain, "active_history", active_at_report)


def test_the_final_report_retries_a_seal_at_its_active_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seal during the report's active read is counted, not undercounted.

    A seal between the report's inventory scan and its active read moves rows
    out of the active file into a source that scan does not list, so the pair
    would count neither copy. The report re-reads the store and describes one
    version, so the sealed rows appear in the count and the span.
    """
    _, store_layout = store_with_archive(tmp_path, 1)
    sealed = False

    def seal_once() -> None:
        """Seal five rows once, between the report's two readings."""
        nonlocal sealed
        if sealed:
            return
        sealed = True
        seal_five_rows(store_layout)

    _at_report_snapshot(monkeypatch, seal_once)

    report = run(tmp_path, policy(segment_bytes=1_000_000, max_age_days=30))

    assert report.status == "ok", "the retried reading describes one version"
    assert report.retained_records == 8, (
        "the archive, the sealed source and the new active rows all count"
    )
    assert report.observed_until == NOW + dt.timedelta(minutes=4), (
        "the span reaches the newest row in the sealed source"
    )


def test_a_writer_that_keeps_sealing_leaves_the_report_unsettled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A report that cannot bind rows to one version is retryable.

    A writer that seals on every reading never lets the report settle. The run
    must report that as retryable rather than recording one of the partial
    readings as a complete report.
    """
    _, store_layout = store_with_archive(tmp_path, 1)
    sequences = itertools.count(2)

    def seal_on_every_read() -> None:
        """Seal one fresh source at every report active-file read."""
        seal_one_row(store_layout, next(sequences))

    _at_report_snapshot(monkeypatch, seal_on_every_read)

    report = run(tmp_path, policy(segment_bytes=1_000_000, max_age_days=30))

    assert report.status == "unsettled", "no reading describes the whole store"
    assert exit_status(report.status) == EXIT_TEMPFAIL, "the timer retries the run"


def _report_after_an_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_records: int | None = None,
) -> RunReport:
    """Append one row after the last retention pass, then report the store.

    Two archived rows and three active rows meet a cap of five exactly; the
    appended fourth active row takes the store to six. The append happens at
    the final report's active read, after retention has finished.

    Parameters
    ----------
    tmp_path : Path
        The test's temporary directory.
    monkeypatch : pytest.MonkeyPatch
        The fixture that replaces the snapshot and active-file seams.
    max_records : int | None
        The record cap the run executes under, or ``None`` for no cap.

    Returns
    -------
    RunReport
        The report the run produced.
    """
    _, store_layout = store_with_archive(tmp_path, 2)
    write_private(store_layout.active, document(records(NOW, 3)))
    grew = False

    def append_one_row() -> None:
        """Append one complete row before the report reads the active file."""
        nonlocal grew
        if grew:
            return
        grew = True
        row = records(NOW, 4)[3]
        with store_layout.active.open("ab") as handle:
            handle.write((outcome_record_encode.encode_line(row) + "\n").encode())

    _at_report_snapshot(monkeypatch, append_one_row)
    return run(
        tmp_path,
        policy(segment_bytes=1_000_000, max_age_days=30, max_records=max_records),
    )


def test_the_report_measures_the_active_version_it_describes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported active bytes come from the final snapshot, not the plan.

    The writer can append after the plan measured the active file and before
    the final report reads it. The report already takes its row count from the
    final reading, so its byte figure must come from that reading too; pairing
    the new count with the plan's older size describes no version of the file.
    """
    report = _report_after_an_append(tmp_path, monkeypatch)

    assert report.active_bytes == len(document(records(NOW, 4))), (
        "the bytes belong to the version the rows were counted from"
    )
    assert report.retained_records == 6, "two archived rows and four active rows"
    assert report.status == "ok", "the settled report describes one version"


def test_the_final_snapshot_reports_retention_it_still_owes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row appended before the report's read leaves the store over its cap.

    The writer never takes the archive lock, so it can append after the last
    retention pass and before the report reads the store. A report that
    claimed ``ok`` would tell the timer the cap is met while the next firing
    still has to evict, so the run reports the store as unsettled and
    retryable.
    """
    report = _report_after_an_append(tmp_path, monkeypatch, max_records=5)

    assert report.retained_records == 6, "the final report counts the appended row"
    assert report.status == "unsettled", "the store is still over its record cap"
    assert exit_status(report.status) == EXIT_TEMPFAIL, "the timer retries the run"
    assert not report.actions, "the final plan is reported, not applied in this run"
