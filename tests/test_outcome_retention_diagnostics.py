"""Diagnostics an operator compares across status, state files and the report.

A retention policy is only useful if the running supervisor and the timer's
last run agree about it, and if a failed maintenance step is visible rather
than silent. These tests pin the reported limits and history, the eviction
cause, the state files' agreement with the report, and the recovery a later
run performs after a state write fails.
"""

import dataclasses as dc
import datetime as dt
import json
from pathlib import Path

from outcome_archive_helpers import (
    NOW,
    archives,
    layout,
    policy,
    records,
    run,
    seeded_store,
    write,
)
from outcome_retention_helpers import settings as _record_settings
from outcome_shell_test_support import (
    FakeWriter,
    OutcomeWorldOptions,
    make_outcome_world,
)
from segment_test_helpers import document, write_private

from buildlimits import config, outcome_archive_cli
from buildlimits.outcome_archive_codec import report_dict
from buildlimits.outcome_archive_evictions import EVICTION_CAUSES
from buildlimits.outcome_archive_exit import EXIT_OK
from buildlimits.outcome_archive_history import ActiveHistory
from buildlimits.outcome_archive_report import Maintenance, RunReport
from buildlimits.outcome_record_encode import header_floor_bytes
from buildlimits.outcome_record_format import MAX_COMPACT_BYTES
from buildlimits.outcome_retention import policy_from_settings
from buildlimits.outcome_retention_report import RetentionLimits
from buildlimits.outcome_segment_inventory import read_inventory
from buildlimits.outcome_segments import segment_name
from buildlimits.status_render import render

# Every cause is always reported, so an expectation only states the one that
# acted and leaves the rest at zero however the vocabulary grows.
NO_EVICTIONS = dict.fromkeys(EVICTION_CAUSES, 0)


def _rohga_settings(tmp_path: Path) -> config.SupervisorConfig:
    """Load the requested 30-day, 15 GB policy from a document.

    Returns
    -------
    SupervisorConfig
        The validated settings.
    """
    return _record_settings(
        tmp_path,
        "cache_outcomes_max_age_days = 30\n"
        "cache_outcomes_max_bytes = 15000000000\n"
        "cache_outcomes_max_records = 0\n",
    )


def _sizes(root: Path) -> tuple[int, int]:
    """Return the first archive's logical size and the active file's size.

    Returns
    -------
    tuple[int, int]
        The archived logical bytes and the active file's bytes.
    """
    files = layout(root)
    archived = read_inventory(files).archived[0]
    return archived.logical_bytes, files.active.stat().st_size


def _state(root: Path) -> tuple[dict[str, object], dict[str, object]]:
    """Read the retention state and the last run report.

    Returns
    -------
    tuple[dict[str, object], dict[str, object]]
        The ``retention.json`` and ``last-run.json`` documents.
    """
    files = layout(root)
    return (
        _document(files.retention_file),
        _document(files.last_run_file),
    )


def _document(path: Path) -> dict[str, object]:
    """Read one JSON state document.

    Returns
    -------
    dict[str, object]
        The decoded document.
    """
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_run_reports_limits_history_and_maintenance(tmp_path: Path) -> None:
    """One document answers what the policy is and what it now retains."""
    seeded_store(tmp_path, records(NOW, 3))
    retention = policy(segment_bytes=1, max_age_days=30, max_bytes=0)

    report = run(tmp_path, retention)
    rendered = report_dict(report)

    assert report.status == "ok", "the run completes"
    assert report.limits is not None, "the policy is reported"
    assert report.limits.as_dict() == rendered["limits"], (
        "the document carries the same limits"
    )
    assert report.limits.max_bytes == 0, "an unlimited budget is reported as zero"
    assert report.limits.byte_eviction is False, "and marked as not evicting"
    assert report.limits.age_eviction is True, "the age bound is still active"
    assert report.retained is not None, "the retained history is reported"
    assert report.retained.record_count == 3, "every archived row is counted"
    assert rendered["retained"] == report.retained.as_dict(), (
        "the document carries the same retained view"
    )
    assert rendered["evictions"] == NO_EVICTIONS, (
        "nothing was evicted under a permissive policy"
    )
    assert rendered["maintenance"] == {"state_written": True, "reason": None}, (
        "the state write succeeded"
    )


def _reported(report: RunReport, subject: str) -> tuple[dict, dict]:
    """Return the report's limits and retained documents.

    Parameters
    ----------
    report : RunReport
        A completed archive run.
    subject : str
        How the assertion failure should name the report.

    Returns
    -------
    tuple[dict, dict]
        The ``limits`` and ``retained`` documents the report carries.
    """
    assert report.limits is not None, f"{subject} reports its policy"
    assert report.retained is not None, f"{subject} reports its history"
    return report.limits.as_dict(), report.retained.as_dict()


def test_the_state_files_agree_with_the_run_report(tmp_path: Path) -> None:
    """The timer's state, the last run and the returned report are one view."""
    seeded_store(tmp_path, records(NOW, 2))

    report = run(tmp_path, policy(max_age_days=30))
    retention, last_run = _state(tmp_path)

    limits, retained = _reported(report, "the run report")

    assert last_run == report_dict(report), "the last run is the report, unchanged"
    assert retention["limits"] == limits, "the state names the same policy"
    assert retention["retained"] == retained, "the state names the same retained view"
    assert retention["retained_logical_bytes"] == retained["logical_bytes"], (
        "the byte total and the retained view describe the same history"
    )


def test_an_age_eviction_names_its_cause(tmp_path: Path) -> None:
    """History past the cutoff is deleted as ``age``, not lumped with bytes.

    The rows are seeded under a policy with no age bound, because the writer
    now expires an aged row when it compacts: a store written under the
    thirty-day bound would already hold none of them.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW - dt.timedelta(days=40), 2), policy(max_age_days=0))

    report = run(tmp_path, policy(max_age_days=30))

    assert report_dict(report)["evictions"] == {**NO_EVICTIONS, "age": 2}, (
        "both rows are attributed to their age alone"
    )
    assert [
        action.reason for action in report.actions if action.action == "deleted"
    ] == ["age", "age"], "each deleted row names the age bound"
    assert report.retained is not None, "the retained history is reported"
    assert report.retained.record_count == 0, "no row survives the cutoff"


def test_a_byte_eviction_names_its_cause(tmp_path: Path) -> None:
    """A budget deletion is reported as ``bytes`` even beside an age limit."""
    seeded_store(tmp_path, records(NOW, 3))
    run(tmp_path, policy())
    one_segment, active = _sizes(tmp_path)

    report = run(tmp_path, policy(max_age_days=30, max_bytes=one_segment * 2 + active))

    assert report_dict(report)["evictions"] == {**NO_EVICTIONS, "bytes": 1}, (
        "the byte bound alone caused the deletion"
    )
    assert [
        action.sequence for action in report.actions if action.action == "deleted"
    ] == [1], "the oldest archive is the one evicted"


def test_a_record_cap_eviction_names_its_cause(tmp_path: Path) -> None:
    """A count-driven deletion names ``records``, not a disabled byte bound."""
    seeded_store(tmp_path, records(NOW, 3))
    unlimited = policy(max_bytes=0)
    run(tmp_path, unlimited)

    report = run(tmp_path, dc.replace(unlimited, max_records=2))

    assert report_dict(report)["evictions"] == {**NO_EVICTIONS, "records": 1}, (
        "the cap alone caused the deletion"
    )
    assert [
        action.reason for action in report.actions if action.action == "deleted"
    ] == ["records"], "the deletion names the bound that removed it"


def test_history_over_two_bounds_names_each_cause(tmp_path: Path) -> None:
    """The age walk trims first, and the byte walk then trims the excess.

    The target architecture defers the byte budget while an age trim is
    pending and re-plans it from the measured history, so one firing can make
    two decisions about the same archive. Each decision names the bound that
    caused it, so an operator can tell which bound released which rows.
    """
    files = layout(tmp_path)
    files.sealed_dir.mkdir(parents=True)
    write_private(files.active, document(()))
    rows = records(NOW - dt.timedelta(days=40), 2) + records(NOW, 2)
    write_private(files.sealed_dir / segment_name(1), document(rows))
    run(tmp_path, policy(max_age_days=0))
    archived, active = _sizes(tmp_path)

    report = run(
        tmp_path,
        policy(max_age_days=30, max_bytes=active + archived // 2),
    )

    assert report_dict(report)["evictions"] == {**NO_EVICTIONS, "age": 1, "bytes": 1}, (
        "the age trim and the byte deletion are counted apart"
    )
    trims = [action for action in report.actions if action.action == "trimmed"]
    assert [action.reason for action in trims] == ["age", "bytes"], (
        "each rewrite names the bound that decided it"
    )
    assert not [action for action in report.actions if action.action == "deleted"], (
        "the byte bound trims the archive rather than removing it"
    )


def test_a_dry_run_reports_limits_and_history_without_state(tmp_path: Path) -> None:
    """Inspection answers the same questions and changes nothing."""
    seeded_store(tmp_path, records(NOW, 2))
    files = layout(tmp_path)

    report = run(tmp_path, policy(max_age_days=30), dry_run=True)

    assert report.dry_run is True, "the run is marked as inspection"
    assert report.limits is not None, "the policy is reported for a dry run"
    assert report.retained is not None, "the current history is reported too"
    assert report.retained.record_count == 2, "both rows are retained"
    assert not files.retention_file.exists(), "no state was written"
    assert not files.last_run_file.exists(), "no last run was recorded"


def test_a_failed_state_write_is_reported_and_recovered(tmp_path: Path) -> None:
    """Work that succeeded is not discarded when its report cannot be saved."""
    seeded_store(tmp_path, records(NOW, 2))
    files = layout(tmp_path)
    # A directory where the state file belongs makes the durable write fail,
    # as a full or read-only filesystem would.
    files.retention_file.mkdir(parents=True)

    report = run(tmp_path, policy())

    assert report.status == "failed", "the timer must retry"
    assert report.reason == "state_write_failed", (
        "the closed reason names the failed write"
    )
    assert report.maintenance == Maintenance(
        state_written=False, reason="state_write_failed"
    ), "maintenance reports the failed write"
    assert [action.action for action in report.actions] == ["archived", "archived"], (
        "the work already done is still reported"
    )
    assert archives(tmp_path), "the archives were published before the failure"
    assert files.retention_file.is_dir(), "no partial state was installed"

    files.retention_file.rmdir()
    recovered = run(tmp_path, policy(max_age_days=30))

    assert recovered.status == "ok", "the next firing completes normally"
    assert recovered.maintenance.state_written is True, (
        "the next firing writes its state"
    )
    assert recovered.limits is not None, "the recovered run reports its limits"
    retention, last_run = _state(tmp_path)
    assert retention["limits"] == recovered.limits.as_dict(), (
        "the recovered state names the same policy"
    )
    assert last_run["status"] == "ok", "the last run records the recovery"


def test_status_and_the_report_agree_on_the_configured_policy(tmp_path: Path) -> None:
    """The supervisor's status limits are the archive report's limits."""
    store = seeded_store(tmp_path, records(NOW, 1))
    loaded = _rohga_settings(tmp_path)

    status = outcome_archive_cli.main([
        "run",
        "--store",
        str(store),
        "--config",
        str(tmp_path / "supervisor.toml"),
    ])
    _, last_run = _state(tmp_path)
    world = make_outcome_world(
        tmp_path,
        OutcomeWorldOptions(writer=_SizedWriter(), policy_settings=loaded),
    )

    document = world.shell.retention()

    assert status == EXIT_OK, "the archive run succeeds"
    assert document is not None, "record mode reports retention"
    limits = document["limits"]
    assert isinstance(limits, dict), "the limits section is a mapping"
    assert limits == last_run["limits"], (
        "status and the last run derive the same limits"
    )
    assert limits == RetentionLimits.of(policy_from_settings(
            loaded,
            header_floor=header_floor_bytes(),
            segment_limit=MAX_COMPACT_BYTES,
        )).as_dict(), (
        "the documented derivation is the one reported"
    )
    assert document["active"] == _SizedWriter.SNAPSHOT.as_dict(), (
        "the active snapshot is the writer's own"
    )
    assert limits["max_bytes"] == 15_000_000_000, (
        "the requested byte budget is reported"
    )
    assert limits["max_records"] is None, "no hidden count cap"


def test_the_status_line_names_the_same_bounds(tmp_path: Path) -> None:
    """The rendered status line is short but carries the same effective limits."""
    document = make_outcome_world(
        tmp_path,
        OutcomeWorldOptions(
            writer=_SizedWriter(), policy_settings=_rohga_settings(tmp_path)
        ),
    ).shell.retention()

    rendered = render({"retention": document, "cache_outcomes": {"enabled": True}})

    assert (
        "cache outcomes retention: 30 days, 15000000000 logical bytes, "
        "no record cap; active 2 records (900 logical bytes)"
    ) in rendered.splitlines(), "the line agrees with the limits document"


class _SizedWriter(FakeWriter):
    """A writer stub whose active file holds a known, complete snapshot."""

    SNAPSHOT = ActiveHistory(
        records=2,
        bytes=900,
        first_observed_at=NOW - dt.timedelta(hours=1),
        last_observed_at=NOW,
    )

    @staticmethod
    def retention() -> ActiveHistory:
        """Return the fixed active snapshot.

        Returns
        -------
        ActiveHistory
            The two rows the status line reports.
        """
        return _SizedWriter.SNAPSHOT
