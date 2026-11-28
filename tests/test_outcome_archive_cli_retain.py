"""The retention-only subcommand's output and state.

The command shares the archive command's configuration, renderer and exit
codes, so these tests state only what the retention operation adds: it writes
its own report, archives nothing, and labels the operation and the clock
decision in bounded operator lines.
"""

import datetime as dt
import json
import typing as typ
from pathlib import Path

from archive_cli_helpers import command_arguments, sealed_measured_source
from outcome_test_helpers import records_at
from segment_test_helpers import document as encode_document

from buildlimits.outcome_archive_cli import main
from buildlimits.outcome_archive_render import write_report
from buildlimits.outcome_archive_report import Maintenance, RunReport
from buildlimits.outcome_segments import SegmentLayout

if typ.TYPE_CHECKING:
    import pytest
    from syrupy.assertion import SnapshotAssertion

    from buildlimits.outcome_record_format import OutcomeRecord

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)


def _aged_config(path: Path, *, days: int) -> Path:
    """Write a configuration whose age bound expires the dated fixtures.

    Returns
    -------
    Path
        The configuration path.
    """
    path.write_text(
        "\n".join([
            'cache_outcomes = "record"',
            'cache_mode = "sccache"',
            f"cache_outcomes_max_age_days = {days}",
            "cache_outcomes_max_bytes = 15000000000",
            "cache_outcomes_max_records = 0",
            "",
        ]),
        encoding="utf-8",
    )
    return path


def _expired_and_fresh_rows() -> tuple[OutcomeRecord, ...]:
    """Build one row outside a thirty-day window and one inside it.

    Returns
    -------
    tuple[OutcomeRecord, ...]
        The expired and fresh rows, in file order.
    """
    now = dt.datetime.now(dt.UTC)
    return records_at(now - dt.timedelta(days=60), 1) + records_at(now, 1)


def _straddling_store(
    tmp_path: Path,
) -> tuple[SegmentLayout, Path, OutcomeRecord]:
    """Seed one store whose source straddles the age bound, and its config.

    Returns
    -------
    tuple[SegmentLayout, Path, OutcomeRecord]
        The store's layout, the configuration path, and the fresh row the
        policy retains.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    expired, fresh = _expired_and_fresh_rows()
    sealed_measured_source(store, 1, (expired, fresh))
    config = _aged_config(tmp_path / "supervisor.toml", days=30)
    return SegmentLayout.beside(store), config, fresh


def test_retain_applies_retention_without_archiving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The retention command rewrites sealed history and creates no archive."""
    layout, config, fresh = _straddling_store(tmp_path)
    store = layout.active

    status = main(["retain", *command_arguments(store, config)])
    output = capsys.readouterr().out

    assert status == 0, "the retention run succeeds"
    assert "operation: retain" in output, "the report names the operation"
    assert "retain: sealed history is rewritten in place, never compressed" in output, (
        "the operator is told what the operation does not do"
    )
    assert layout.last_retain_file.exists(), "the retention report is recorded"
    assert not layout.last_run_file.exists(), "the archive report is not replaced"
    reported = json.loads(layout.last_retain_file.read_text(encoding="utf-8"))
    assert reported["operation"] == "retain", "the stored report names the operation"
    assert not list(layout.archive_dir.glob("*.xz")), "no archive was created"
    assert (layout.sealed_dir / "segment-000000000001.jsonl").read_bytes() == (
        encode_document((fresh,))
    ), "the expired row is gone and the fresh row survives"


def test_retain_reports_its_plan_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The JSON document carries the operation and the retention plan."""
    store = tmp_path / "cache-outcomes.jsonl"
    rows = records_at(dt.datetime.now(dt.UTC) - dt.timedelta(days=60), 1)
    sealed_measured_source(store, 1, rows)
    config = _aged_config(tmp_path / "supervisor.toml", days=30)

    status = main(["retain", *command_arguments(store, config), "--json"])
    document = json.loads(capsys.readouterr().out)

    assert status == 0, "the run succeeds"
    assert document["operation"] == "retain", "the operation is published"
    assert document["clock_skew"] is False, "a quiet host reports no jump"
    assert document["plan"]["delete"] == 1, "the whole-segment eviction is planned"


def test_retain_dry_run_leaves_the_store_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A retention preview plans without rewriting or recording state."""
    layout, config, _fresh = _straddling_store(tmp_path)
    store = layout.active
    before = (layout.sealed_dir / "segment-000000000001.jsonl").read_bytes()

    status = main(["retain", "--dry-run", *command_arguments(store, config)])
    output = capsys.readouterr().out

    assert status == 0, "the preview succeeds"
    assert "dry run: no file was changed" in output, "the preview says so"
    assert (layout.sealed_dir / "segment-000000000001.jsonl").read_bytes() == before, (
        "the source is untouched"
    )
    assert not layout.last_retain_file.exists(), "a preview records no report"


def test_a_skewed_report_labels_the_withheld_age_bound(
    capsys: pytest.CaptureFixture[str],
    snapshot: SnapshotAssertion,
) -> None:
    """The plain report names the guard and never claims age is disabled."""
    report = RunReport(
        status="ok",
        operation="retain",
        clock_skew=True,
        cutoff=None,
        started_at=NOW,
        finished_at=NOW,
    )

    write_report(report, as_json=False)
    output = capsys.readouterr().out

    assert output == snapshot, "the guarded report's lines are a stable contract"
    assert "cutoff: withheld by clock skew" in output, (
        "the withheld bound is not reported as disabled"
    )


def test_a_preview_detects_a_clock_jump_without_claiming_a_withheld_bound(
    capsys: pytest.CaptureFixture[str],
    snapshot: SnapshotAssertion,
) -> None:
    """A preview applies the age bound, so it names detection, not withholding."""
    report = RunReport(
        status="ok",
        operation="retain",
        clock_skew=True,
        dry_run=True,
        cutoff=NOW + dt.timedelta(days=1),
        started_at=NOW,
        finished_at=NOW,
    )

    write_report(report, as_json=False)
    output = capsys.readouterr().out

    assert output == snapshot, "the preview's lines are a stable contract"


def test_a_report_withholds_the_baseline_line_when_the_state_was_written(
    capsys: pytest.CaptureFixture[str],
    snapshot: SnapshotAssertion,
) -> None:
    """A failed baseline write is named without claiming the state failed."""
    report = RunReport(
        status="failed",
        operation="retain",
        reason="clock_baseline_failed",
        started_at=NOW,
        finished_at=NOW,
        maintenance=Maintenance(
            state_written=True,
            baseline_written=False,
            reason="clock_baseline_failed",
        ),
    )

    write_report(report, as_json=False)
    output = capsys.readouterr().out

    assert output == snapshot, "the maintenance line is a stable contract"
