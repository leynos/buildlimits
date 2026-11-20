"""Run times taken from the injected clock rather than the system wall.

Every status the timer acts on carries a start and a finish time. Reading the
wall clock directly would make a run's duration unreproducible and could stamp
a finish before its start when the host clock steps, so the executed report,
the retryable report and the busy report all date themselves from the one
injected source.
"""

import datetime as dt
import json
import typing as typ
from pathlib import Path

from outcome_archive_helpers import NOW, layout, policy, records, write

from buildlimits import outcome_archive_retain
from buildlimits.outcome_archive import RunOptions, run_archive
from buildlimits.outcome_archive_lock import ArchiveLock

if typ.TYPE_CHECKING:
    import pytest

    from buildlimits.outcome_archive_history import ActiveHistory
    from buildlimits.outcome_segment_inventory import Inventory
    from buildlimits.outcome_segments import SegmentLayout


def test_the_run_reads_both_its_times_from_the_injected_clock(tmp_path: Path) -> None:
    """The start and finish come from the supplied clock, not the system wall.

    Every status the timer acts on carries a finish time. A direct wall-clock
    read would make the run's duration unreproducible and could stamp a finish
    before the start when the host clock steps, so both readings come from one
    injected source.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())
    first = NOW + dt.timedelta(seconds=10)
    second = NOW + dt.timedelta(seconds=30)
    times = iter((first, second))

    report = run_archive(
        layout(tmp_path), policy(), RunOptions(clock=lambda: next(times))
    )

    assert report.status == "ok", "the run completed"
    assert report.started_at == first, "the start is the clock's first reading"
    assert report.finished_at == second, "the finish is the clock's second reading"
    last_run = json.loads(layout(tmp_path).last_run_file.read_text(encoding="utf-8"))
    assert last_run["finished_at"] == "2026-11-19T00:00:30Z", (
        "the persisted state carries the same finish time"
    )


def test_an_unsettled_run_stamps_the_injected_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retryable report is dated by the same clock as an executed run.

    The unsettled report is built before any plan exists, so it has no report
    body whose finish time it could inherit; it must read the injected clock.
    """
    finished = NOW + dt.timedelta(seconds=5)
    settle = outcome_archive_retain.settled_snapshot

    def report_unsettled(
        store_layout: SegmentLayout,
        floor: int | None,
        *,
        cutoff: dt.datetime | None = None,
    ) -> tuple[Inventory, ActiveHistory, bool]:
        """Return the real reading marked as never having settled."""
        inventory, active, _settled = settle(store_layout, floor, cutoff=cutoff)
        return inventory, active, False

    monkeypatch.setattr(outcome_archive_retain, "settled_snapshot", report_unsettled)

    report = run_archive(
        layout(tmp_path),
        policy(),
        RunOptions(now=NOW, dry_run=True, clock=lambda: finished),
    )

    assert report.status == "unsettled", "no reading settled"
    assert report.finished_at == finished, "the retryable report uses the clock"


def test_a_busy_run_stamps_the_injected_clock(tmp_path: Path) -> None:
    """A run that never took its lock still reports the supplied finish time."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())
    finished = NOW + dt.timedelta(seconds=1)

    with ArchiveLock(layout(tmp_path).lock_file):
        report = run_archive(
            layout(tmp_path),
            policy(),
            RunOptions(now=NOW, clock=lambda: finished),
        )

    assert report.status == "busy", "the run did not start"
    assert report.finished_at == finished, "the busy report uses the clock"
