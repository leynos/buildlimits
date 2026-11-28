"""The work a follow-up retention pass must still perform.

A run performs several bounded passes, and each must not repeat an applied
trim, retry a failed step, or invert the oldest-first order. These tests pin
the bookkeeping that decides those three questions, including the difference
between an entry that is proven gone and one the process may no longer inspect.
"""

import typing as typ
from pathlib import Path

from outcome_archive_helpers import NOW, layout, policy
from outcome_test_helpers import records_at
from segment_test_helpers import document, write_private

from buildlimits import outcome_archive_pending
from buildlimits.outcome_archive_context import ActionResult
from buildlimits.outcome_archive_plan import Plan
from buildlimits.outcome_archive_run import ArchiveRun, RunOutcome
from buildlimits.outcome_archive_summary import PlanSummary
from buildlimits.outcome_segment_inventory import SegmentEntry
from buildlimits.outcome_segments import segment_name

if typ.TYPE_CHECKING:
    import pytest

    from buildlimits.outcome_archive_trim import Trim


def _entry(root: Path, sequence: int) -> SegmentEntry:
    """Build one sealed entry whose path the test may or may not create.

    Returns
    -------
    SegmentEntry
        The entry.
    """
    return SegmentEntry(
        sequence=sequence,
        path=layout(root).sealed_dir / segment_name(sequence),
        state="sealed",
        physical_bytes=10,
        logical_bytes=10,
        record_count=1,
        has_metadata=True,
    )


def _run(root: Path, plan: Plan) -> ArchiveRun:
    """Bind one plan to the shared test context.

    Returns
    -------
    ArchiveRun
        The run the bookkeeping reads.
    """
    from buildlimits.outcome_archive_context import ArchiveContext

    context = ArchiveContext(
        layout=layout(root),
        policy=policy(1_000_000),
        preset=0,
        now=NOW,
        quarantine_token=lambda: "0123456789ab",
        clock=lambda: NOW,
    )
    return ArchiveRun(context=context, plan=plan, summary=PlanSummary(), started_at=NOW)


def _trim() -> Trim:
    """Return one inert trim for a planned rewrite.

    Returns
    -------
    Trim
        The prefix policy.
    """
    from buildlimits.outcome_archive_trim import Trim

    return Trim()


def test_a_completed_removal_is_not_an_eviction_barrier(tmp_path: Path) -> None:
    """A failure recorded beside a completed deletion does not block newer work.

    A deletion that removes the file and then fails its directory sync records
    both a failure and a ``deleted`` action. The older entry is gone, so the
    newer steps the plan ordered after it may run.
    """
    first = _entry(tmp_path, 1)
    second = _entry(tmp_path, 2)
    run = _run(
        tmp_path,
        Plan(
            trims=((first, _trim()), (second, _trim())),
            deletes=(first, second),
        ),
    )
    outcome = RunOutcome(
        actions=[ActionResult("deleted", 1)],
        failures=[(1, "unreadable")],
    )

    assert outcome_archive_pending.failed_eviction(run, outcome) is None, (
        "a sequence that was really removed is no barrier"
    )


def test_a_failure_without_its_deletion_still_blocks_newer_work(
    tmp_path: Path,
) -> None:
    """An entry that may still exist keeps the oldest-first barrier."""
    first = _entry(tmp_path, 1)
    second = _entry(tmp_path, 2)
    run = _run(
        tmp_path,
        Plan(deletes=(first, second), trims=((second, _trim()),)),
    )
    outcome = RunOutcome(failures=[(1, "unreadable")])

    assert outcome_archive_pending.failed_eviction(run, outcome) == 1, (
        "the older sequence the budget asked for still blocks newer steps"
    )


def _narrowed(root: Path, plan: Plan) -> Plan:
    """Narrow one plan with no completed or failed work behind it.

    Returns
    -------
    Plan
        The steps a fresh pass may perform.
    """
    run = _run(root, plan)
    return outcome_archive_pending.narrow(
        run.plan,
        outcome_archive_pending.Done(
            failed=set(), blocked=None, applied={}, removed=set()
        ),
    )


def test_an_uninspectable_entry_is_not_treated_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a proven absence drops a step; an inspection fault keeps it.

    ``Path.exists`` reports ``False`` for every ``OSError``, so a directory the
    process may no longer inspect would silently lose its planned deletion and
    let a newer one run first.
    """
    entry = _entry(tmp_path, 1)
    write_private(entry.path, document(records_at(NOW, 1)))
    monkeypatch.setattr(
        outcome_archive_pending, "is_proven_absent", lambda _path: False
    )

    narrowed = _narrowed(tmp_path, Plan(deletes=(entry,)))

    assert narrowed.deletes == (entry,), "the step survives an inspection fault"


def test_a_proven_absent_entry_is_dropped(tmp_path: Path) -> None:
    """An entry that is confirmed gone has no step left to perform."""
    entry = _entry(tmp_path, 1)

    narrowed = _narrowed(tmp_path, Plan(deletes=(entry,)))

    assert narrowed.deletes == (), "a missing entry is not acted on again"
    assert not outcome_archive_pending.has_work(narrowed), "nothing remains"
