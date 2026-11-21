"""The command's configuration and store are resolved when it runs.

A systemd timer starts the command in the environment the unit defines, so the
default configuration path and the store path must both follow that environment
rather than the one that imported the module. These tests prove each default is
read at run time by replacing the environment a run-time lookup would use.
"""

import datetime as dt
import json
import typing as typ
from pathlib import Path

from archive_cli_helpers import sealed_source, write_config
from outcome_test_helpers import records_at
from segment_test_helpers import document as encode_document

from buildlimits.outcome_archive_cli import main

if typ.TYPE_CHECKING:
    import pytest

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)


def test_the_default_configuration_follows_the_run_time_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The default config path is resolved when the command runs.

    A module-level ``Path.home()`` would freeze the home of whichever account
    imported the module, so a timer running under another account would read
    the wrong configuration. Rejecting a value only the run-time home holds
    proves the path follows the environment the command actually starts in.
    """
    home = tmp_path / "home"
    config = home / ".config" / "buildlimits" / "supervisor.toml"
    config.parent.mkdir(parents=True)
    config.write_text("cache_outcomes_max_bytes = 1\n")
    store = tmp_path / "cache-outcomes.jsonl"
    sealed_source(store, 1, encode_document(records_at(NOW, 1)))
    monkeypatch.setenv("HOME", str(home))

    status = main(["inspect", "--store", str(store)])
    error = capsys.readouterr().err

    assert status == 2, "the default configuration was read and rejected"
    assert "cache_outcomes" in error, "the rejected key is named"


def test_the_store_is_resolved_from_the_run_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no ``--store``, the command reads the environment it runs under.

    The store path is resolved at run time, so a timer's environment selects
    the store. The plan counts the source in that store and nowhere else.
    """
    state = tmp_path / "state"
    store = state / "buildlimits" / "cache-outcomes.jsonl"
    sealed_source(store, 1, encode_document(records_at(NOW, 1)))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = write_config(tmp_path / "supervisor.toml")

    status = main(["inspect", "--config", str(config), "--json"])
    report = json.loads(capsys.readouterr().out)

    assert status == 0, "inspection succeeds"
    assert report["plan"]["archive"] == 1, "the environment's store was read"
