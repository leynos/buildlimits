"""The stable JSON document a report is published in, field by field."""

import datetime as dt
import typing as typ

from buildlimits.outcome_archive_codec import ARCHIVE_SCHEMA, action_dict, report_dict
from buildlimits.outcome_archive_context import ActionResult
from buildlimits.outcome_archive_evictions import EVICTION_CAUSES
from buildlimits.outcome_archive_plan import PlanSummary
from buildlimits.outcome_archive_report import Maintenance, RunReport
from buildlimits.outcome_retention import RetentionPolicy
from buildlimits.outcome_retention_history import RetainedHistory
from buildlimits.outcome_retention_report import RetentionLimits

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

_NOW = dt.datetime(2026, 11, 19, 12, 0, 0, tzinfo=dt.UTC)
_LATER = _NOW + dt.timedelta(seconds=3)
_EXPECTED_KEYS = {
    "schema",
    "operation",
    "status",
    "clock_skew",
    "clock_baseline_unreadable",
    "dry_run",
    "started_at",
    "finished_at",
    "active_bytes",
    "retained_logical_bytes",
    "retained_fallback_bytes",
    "retained_records",
    "observed_from",
    "observed_until",
    "unmeasured_entries",
    "budget_bytes",
    "cutoff",
    "plan",
    "actions",
    "failures",
    "gaps",
    "gaps_truncated",
    "missing_sidecars",
    "corrupt_sidecars",
    "orphan_sidecars",
    "sealed_replacing",
    "evictions",
    "limits",
    "retained",
    "maintenance",
    "reason",
}


_RETAINED = RetainedHistory(
    record_count=3,
    logical_bytes=22,
    active_bytes=11,
    sealed_bytes=5,
    archived_logical_bytes=6,
    archived_physical_bytes=4,
    closed_segments=1,
    active_records=3,
    first_observed_at=_NOW,
    last_observed_at=_LATER,
    complete=False,
)


def _report() -> RunReport:
    """Build one report carrying a value in every field."""
    return RunReport(
        status="corrupt",
        operation="retain",
        clock_skew=True,
        clock_baseline_unreadable=True,
        started_at=_NOW,
        finished_at=_LATER,
        dry_run=True,
        active_bytes=11,
        retained_bytes=22,
        retained_records=3,
        observed_from=_NOW,
        observed_until=_LATER,
        unmeasured_entries=1,
        budget_bytes=44,
        cutoff=_NOW - dt.timedelta(days=30),
        plan=PlanSummary(archive=1, trim=2, delete=3, retained_bytes=55),
        actions=(ActionResult("trimmed", 7, logical_bytes=8, physical_bytes=9),),
        failures=((5, "corrupt"),),
        gaps=(2, 4),
        gaps_truncated=True,
        missing_sidecars=(6,),
        corrupt_sidecars=(7,),
        orphan_sidecars=(8,),
        sealed_replacing=(9,),
        limits=RetentionLimits.of(RetentionPolicy()),
        retained=_RETAINED,
        maintenance=Maintenance(state_written=False, reason="state_write_failed"),
        reason="unreadable",
    )


def test_the_report_document_has_exactly_the_documented_fields() -> None:
    """Every published field is present, and nothing else is."""
    document = report_dict(_report())
    empty = report_dict(
        RunReport(status="unsettled", started_at=_NOW, finished_at=_NOW)
    )

    assert set(document) == _EXPECTED_KEYS, "the document's key set is fixed"
    assert set(empty) == _EXPECTED_KEYS, "the key set does not depend on values"
    assert document["schema"] == ARCHIVE_SCHEMA, "the schema version is published"
    assert document["operation"] == "retain", "the operation is published"
    assert document["status"] == "corrupt", "the closed status is published"
    assert document["clock_skew"] is True, "the clock decision is published"
    assert document["clock_baseline_unreadable"] is True, (
        "a baseline read failure is published too"
    )
    assert document["dry_run"] is True, "the dry-run marker is published"
    assert document["started_at"] == "2026-11-19T12:00:00Z", "UTC text, not a value"
    assert document["finished_at"] == "2026-11-19T12:00:03Z", "UTC text, not a value"
    assert document["active_bytes"] == 11, "the active bytes are published"
    assert document["retained_logical_bytes"] == 22, "the retained bytes are published"
    assert document["retained_fallback_bytes"] == 0, (
        "a measured report has no physical-size fallback"
    )
    assert document["retained_records"] == 3, "the retained count is published"
    assert document["observed_from"] == "2026-11-19T12:00:00Z", "the span start"
    assert document["observed_until"] == "2026-11-19T12:00:03Z", "the span end"
    assert document["unmeasured_entries"] == 1, "the unmeasured count is published"
    assert document["budget_bytes"] == 44, "the budget is published"
    assert document["cutoff"] == "2026-10-20T12:00:00Z", "the cutoff is published"


def test_the_document_renders_the_integrity_and_state_fields() -> None:
    """Failures, gaps, sidecars and the installed state keep their shapes."""
    document = report_dict(_report())
    empty = report_dict(
        RunReport(status="unsettled", started_at=_NOW, finished_at=_NOW)
    )

    assert document["failures"] == [{"sequence": 5, "reason": "corrupt"}], (
        "each failed sequence keeps its closed reason"
    )
    assert document["gaps"] == [2, 4], "the gaps are published in order"
    assert document["gaps_truncated"] is True, "the truncation flag is published"
    assert document["missing_sidecars"] == [6], "the missing sidecars are published"
    assert document["corrupt_sidecars"] == [7], "the corrupt sidecars are published"
    assert document["orphan_sidecars"] == [8], "the orphan sidecars are published"
    assert document["sealed_replacing"] == [9], (
        "unfinished sealed rewrites are published"
    )
    assert document["evictions"] == dict.fromkeys(EVICTION_CAUSES, 0), (
        "every eviction cause is counted, even at zero"
    )
    assert document["limits"] == RetentionLimits.of(RetentionPolicy()).as_dict(), (
        "the effective limits are published"
    )
    assert document["retained"] == _RETAINED.as_dict(), (
        "the retained history is published"
    )
    assert document["maintenance"] == {
        "state_written": False,
        "baseline_written": True,
        "reason": "state_write_failed",
    }, "a failed state write is named in the document"
    assert document["reason"] == "unreadable", "the run-level reason is published"
    assert empty["plan"] is None, "an absent plan is null"
    assert empty["cutoff"] is None, "an absent cutoff is null"
    assert empty["observed_from"] is None, "an absent span start is null"
    assert empty["observed_until"] is None, "an absent span end is null"
    assert empty["actions"] == [], "no action is an empty list"
    assert empty["failures"] == [], "no failure is an empty list"
    assert empty["reason"] is None, "an absent reason is null"
    assert empty["limits"] is None, "an underived policy is null"
    assert empty["retained"] is None, "an unmeasured history is null"
    assert empty["maintenance"] == {
        "state_written": True,
        "baseline_written": True,
        "reason": None,
    }, "a report built without state still says nothing failed"
    assert empty["operation"] == "archive", "the default operation is archival"
    assert empty["clock_skew"] is False, "a fresh report has no clock decision"
    assert empty["clock_baseline_unreadable"] is False, (
        "a fresh report has no baseline fault"
    )


def test_the_document_renders_the_plan_and_actions(
    snapshot: SnapshotAssertion,
) -> None:
    """The plan summary and each action keep their own stable shapes."""
    document = report_dict(_report())

    assert document["plan"] == snapshot(name="plan"), (
        "the plan's counts and estimate are fixed"
    )
    assert document["actions"] == snapshot(name="actions"), (
        "the action list is a fixed shape"
    )


def test_an_action_carries_its_fault_reason(
    snapshot: SnapshotAssertion,
) -> None:
    """A quarantined action publishes the closed reason it recorded."""
    action = ActionResult("quarantined", 4, reason="bad_xz")

    assert action_dict(action) == snapshot, "the fault reason survives"
