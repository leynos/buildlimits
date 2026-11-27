"""An inspection reports the bytes it still holds, not the plan's estimate."""

import datetime as dt
import json
from pathlib import Path

import pytest
from archive_cli_helpers import command_arguments, sealed_measured_source, write_config
from outcome_archive_helpers import NOW as RUN_NOW
from outcome_archive_helpers import layout, policy, records, refuse, run, write
from outcome_test_helpers import records_at

from buildlimits import outcome_archive_files
from buildlimits.outcome_archive_cli import main
from buildlimits.outcome_archive_codec import report_dict
from buildlimits.outcome_archive_render import write_report
from buildlimits.outcome_segments import archive_name, sidecar_name

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)


def _fixture(tmp_path: Path) -> tuple[Path, Path, int]:
    """Build a store with two measured sources and a plan that deletes one."""
    store = tmp_path / "cache-outcomes.jsonl"
    first = sealed_measured_source(store, 1, records_at(NOW, 1))
    second = sealed_measured_source(store, 2, records_at(NOW, 1))
    config = write_config(tmp_path / "supervisor.toml", max_records=1)
    active = store.stat().st_size if store.exists() else 0
    current = first.stat().st_size + second.stat().st_size + active
    return store, config, current


@pytest.mark.parametrize("command", ["inspect", "run"])
def test_a_planned_eviction_still_reports_the_current_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    """The plain report names the bytes eviction has not removed yet."""
    store, config, current = _fixture(tmp_path)
    arguments = [command, *command_arguments(store, config)]
    if command == "run":
        arguments.append("--dry-run")

    status = main(arguments)
    output = capsys.readouterr().out

    assert status == 0, "the inspection succeeds"
    assert f"retained: {current} of 15000000000 logical bytes" in output, (
        "the retained figure is the store's current bytes"
    )
    assert "delete 1" in output, "the plan still reports the projected deletion"


def test_the_json_keeps_the_current_bytes_apart_from_the_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The document's retained figure and the plan's estimate are distinct."""
    store, config, current = _fixture(tmp_path)

    status = main(["inspect", *command_arguments(store, config), "--json"])
    document = json.loads(capsys.readouterr().out)

    assert status == 0, "the inspection succeeds"
    assert document["retained_logical_bytes"] == current, (
        "the document reports the bytes the store still holds"
    )
    assert document["retained_records"] == 2, "both sources are still retained"
    assert document["plan"]["delete"] == 1, "the plan projects one deletion"
    assert document["plan"]["retained_logical_bytes"] < current, (
        "the plan's estimate is the post-eviction figure"
    )


def _published_archive_without_sidecar(tmp_path: Path) -> Path:
    """Publish one archive, remove its sidecar, and return the archive path."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(RUN_NOW, 1), policy())
    first = run(tmp_path, policy())
    assert first.status == "ok", "the fixture archive is published"
    store_layout = layout(tmp_path)
    (store_layout.archive_dir / sidecar_name(1)).unlink()
    return store_layout.archive_dir / archive_name(1)


def test_a_missing_sidecar_discloses_the_physical_size_fallback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unmeasured archive's bytes are named as a physical-size estimate."""
    archive = _published_archive_without_sidecar(tmp_path)

    report = run(tmp_path, policy(), dry_run=True)
    document = report_dict(report)

    assert report.unmeasured_entries == 1, "the archive is counted as unmeasured"
    assert report.fallback_bytes == archive.stat().st_size, (
        "its physical size is the substituted figure"
    )
    assert document["retained_fallback_bytes"] == report.fallback_bytes, (
        "the JSON names the fallback component"
    )

    write_report(report, as_json=False)
    output = capsys.readouterr().out
    assert (
        f"retained fallback: {report.fallback_bytes} bytes from physical size; "
        "1 entries unmeasured"
    ) in output, "the plain report names the fallback component"


def test_an_executed_report_discloses_an_unrepaired_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repair that fails leaves the archive counted by its physical size."""
    archive = _published_archive_without_sidecar(tmp_path)
    monkeypatch.setattr(outcome_archive_files, "repair_sidecar", refuse)

    report = run(tmp_path, policy())

    assert report.status == "corrupt", "the failed repair needs an operator"
    assert report.unmeasured_entries == 1, "the archive remains unmeasured"
    assert report.fallback_bytes == archive.stat().st_size, (
        "the executed report discloses the substituted physical size"
    )
