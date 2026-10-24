"""Pin the captured FizzBee output to what the pinned checker prints today.

The fixtures under ``tests/fixtures/fizzbee`` are the RFC 0001 record of real
checker runs, and the models that produced them are the spike's. A FizzBee
version bump that changes fairness, liveness or graph semantics must fail
here rather than quietly invalidate the record, so each captured class is
re-run and compared byte for byte.

Like every checker test these are opt-in: without
``BUILD_LIMITS_MODEL_CHECK=1`` ``tests/conftest.py`` skips the module.
"""

import json
import typing as typ
from pathlib import Path

import pytest
from fizzbee_spike import (
    OPT_IN,
    error_graph,
    missing_opt_in_reason,
    run_model,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

FIXTURES = Path(__file__).with_name("fixtures") / "fizzbee"

# A plain assignment here would be reported by the DF12 re-export rule, so
# the mark is written as a list the way the systemd modules do it.
pytestmark = [pytest.mark.fizzbee]

# Every captured class and the model that produced it. The parse-error class
# has no error graph and is covered by the spike suite's own test, because it
# writes nothing at all.
_CAPTURED = (
    ("pass", "continuously_enabled.fizz"),
    ("safety-failure", "safety_failure.fizz"),
    ("lasso", "liveness_strict.fizz"),
    ("stutter", "liveness_stutter.fizz"),
    ("deadlock", "deadlock.fizz"),
    ("truncation-safety", "truncation_safety.fizz"),
    ("truncation-liveness", "truncation_liveness.fizz"),
)

# Of those, the classes whose run fails and so leaves an error graph.
_WITH_ERROR_GRAPH = (
    "safety-failure",
    "lasso",
    "stutter",
    "deadlock",
    "truncation-liveness",
)


def _captured(output_class: str) -> str:
    """Return a captured fixture's standard output.

    Parameters
    ----------
    output_class : str
        Directory under ``tests/fixtures/fizzbee``.

    Returns
    -------
    str
        The recorded output with the same volatile figures masked.
    """
    return (FIXTURES / output_class / "stdout.txt").read_text(encoding="utf-8")


def _captured_graph(output_class: str) -> cabc.Sequence[dict[str, typ.Any]]:
    """Return a captured fixture's parsed error graph.

    Parameters
    ----------
    output_class : str
        Directory under ``tests/fixtures/fizzbee`` with an ``error-graph.json``.

    Returns
    -------
    Sequence[dict[str, Any]]
        The recorded links.
    """
    payload = (FIXTURES / output_class / "error-graph.json").read_text(encoding="utf-8")
    return json.loads(payload)


@pytest.mark.timeout(120)
def test_captured_fixtures_match_the_live_output(tmp_path: Path) -> None:
    """Every captured fixture still describes what the pinned checker prints."""
    for output_class, model in _CAPTURED:
        run = run_model(tmp_path / output_class, model)
        assert run.stdout == _captured(output_class), output_class


@pytest.mark.timeout(120)
def test_captured_error_graphs_match_the_live_ones(tmp_path: Path) -> None:
    """The recorded error graphs are the ones the checker writes today."""
    models = dict(_CAPTURED)
    for output_class in _WITH_ERROR_GRAPH:
        run = run_model(tmp_path / output_class, models[output_class])
        assert error_graph(run) == list(_captured_graph(output_class)), output_class


def test_opt_in_is_named_in_the_skip_reason() -> None:
    """The variable a developer must set is spelled out in the reason.

    This one runs under either setting: with the opt-in there is no reason to
    inspect, so the check moves to the variable the module names.
    """
    reason = missing_opt_in_reason()
    if reason is None:
        assert OPT_IN == "BUILD_LIMITS_MODEL_CHECK", "the variable is spelled out"
    else:
        assert OPT_IN in reason, "the skip reason names the variable to set"
