"""Ensure ordinary test collection cannot run the model checker.

These tests must pass under either setting of the opt-in, because the ordinary
suite runs without it and the model-check target runs with it.
"""

from pathlib import Path
from unittest import mock

import pytest
from fizzbee_spike import OPT_IN, missing_opt_in_reason, run_model


def test_model_check_probe_requires_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Name the opt-in variable, and nothing else, as the reason to skip."""
    monkeypatch.delenv(OPT_IN, raising=False)
    reason = missing_opt_in_reason()
    assert reason is not None, "the checker must not run without the opt-in"
    assert OPT_IN in reason, "the reason must name the variable to set"


def test_model_check_probe_accepts_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value of ``1`` is the only one that opts in."""
    monkeypatch.setenv(OPT_IN, "1")
    assert missing_opt_in_reason() is None, "a value of 1 opts in"


def test_model_check_probe_rejects_other_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any other value leaves the run opted out, so a stray ``true`` is safe."""
    monkeypatch.setenv(OPT_IN, "true")
    assert missing_opt_in_reason() is not None, "only the value 1 opts in"


def test_run_model_spawns_nothing_without_the_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail before spawning anything when the pinned binary is absent.

    A skip without the opt-in is the collection hook's job. Once a test has
    decided to run, a missing binary is a failure: an opted-in run that
    checked nothing must not report success.
    """
    import fizzbee_spike

    spawn = mock.Mock(side_effect=AssertionError("must not spawn a checker"))
    monkeypatch.setattr(fizzbee_spike.subprocess, "Popen", spawn)
    monkeypatch.setattr(fizzbee_spike, "FIZZ", tmp_path / "absent" / "fizz")
    with pytest.raises(pytest.fail.Exception, match="make fizzbee"):
        run_model(tmp_path, "collections.fizz")
    spawn.assert_not_called()


def test_fizzbee_marker_is_registered(request: pytest.FixtureRequest) -> None:
    """The marker is registered, so collecting the module is warning-free."""
    marker = request.config.getini("markers")
    assert any(entry.startswith("fizzbee:") for entry in marker), (
        "the marker is registered in pyproject.toml"
    )
