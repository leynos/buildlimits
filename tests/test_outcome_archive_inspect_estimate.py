"""An inspection reports the bytes it still holds, not the plan's estimate."""

import datetime as dt
import json
from pathlib import Path

import pytest
from archive_cli_helpers import command_arguments, sealed_measured_source, write_config
from outcome_test_helpers import records_at

from buildlimits.outcome_archive_cli import main

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
