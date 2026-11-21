"""The settled report builder maps values, and needs no store to do it."""

import datetime as dt
from pathlib import Path

from buildlimits.outcome_archive_context import ArchiveContext
from buildlimits.outcome_archive_history import ActiveHistory, SettledStore
from buildlimits.outcome_archive_plan import Plan, PlanSummary
from buildlimits.outcome_archive_report import settled_report
from buildlimits.outcome_archive_run import ArchiveRun, RunOutcome
from buildlimits.outcome_retention import RetentionPolicy
from buildlimits.outcome_segment_inventory import Inventory
from buildlimits.outcome_segments import SegmentLayout

_NOW = dt.datetime(2026, 11, 19, 12, 0, 0, tzinfo=dt.UTC)


def _run() -> ArchiveRun:
    """Build one run whose paths are never opened."""
    context = ArchiveContext(
        layout=SegmentLayout.beside(Path("/state/cache-outcomes.jsonl")),
        policy=RetentionPolicy(),
        preset=0,
        now=_NOW,
        quarantine_token=lambda: "0123456789ab",
        clock=lambda: _NOW,
    )
    return ArchiveRun(
        context=context, plan=Plan(), summary=PlanSummary(), started_at=_NOW
    )


def test_the_builder_maps_a_settled_store_without_reading_one() -> None:
    """The builder needs only values, so a pure caller can use it."""
    report = settled_report(
        _run(), RunOutcome(), SettledStore(Inventory(), ActiveHistory(), False)
    )

    assert report.status == "ok", "a settled run with no failure is ok"
    assert report.retained_records == 0, "the empty settled store holds no rows"
    assert report.retained_bytes == 0, "the empty settled store holds no bytes"
    assert not report.gaps, "the empty inventory reports no gap"
    assert report.gaps_truncated is False, "the empty inventory truncates nothing"


def test_the_builder_reports_retention_the_caller_measured_as_owed() -> None:
    """The retention decision reaches only the status classification."""
    report = settled_report(
        _run(), RunOutcome(), SettledStore(Inventory(), ActiveHistory(), True)
    )

    assert report.status == "unsettled", "the caller's decision is honoured"
