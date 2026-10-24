"""Assert the files a passing run and a failing run write, and how big they get.

A verdict alone says what the checker decided; these tests pin the artifacts
and the figures RFC 0001 relies on, so a version bump that changes an output
format, an exit code, or a state count fails here rather than silently
invalidating the record. The exit-code questions live here because they are
read off the same runs.

Like every checker test these are opt-in: without ``BUILD_LIMITS_MODEL_CHECK=1``
``tests/conftest.py`` skips the module.
"""

import typing as typ
from pathlib import Path

import pytest
from fizzbee_expectations import (
    FAILED,
    FAILED_LIVENESS,
    PASSED,
    Expectation,
    check,
)
from fizzbee_spike import error_graph, link_names, run_model

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

# The collector hook in ``tests/conftest.py`` reads this mark, so every test
# module that runs the checker has to carry it. A list is what the systemd
# modules use, and the DF12 re-export rule wants it spelled this way.
pytestmark = [pytest.mark.fizzbee]


@pytest.mark.timeout(120)
def test_pass_writes_a_graph_with_pointer_node_ids(tmp_path: Path) -> None:
    """A pass writes ``graph.dot`` with pointer-style ids and no error graph."""
    run = check(
        tmp_path, Expectation("continuously_enabled.fizz", PASSED, "the model passes")
    )
    graph = (run.output / "graph.dot").read_text(encoding="utf-8")
    assert '"node0"' in graph or '"0x' in graph, "node ids are pointers or node0"
    assert not (run.output / "error-graph.json").exists(), (
        "a passing run writes no error graph"
    )


@pytest.mark.timeout(120)
def test_error_graph_link_schema(tmp_path: Path, snapshot: SnapshotAssertion) -> None:
    """A failing run's ``error-graph.json`` carries the documented fields."""
    run = check(
        tmp_path,
        Expectation(
            "safety_failure.fizz",
            FAILED,
            f"{FAILED}. Invariant:  NeverFull names the failing invariant",
        ),
    )
    graph = error_graph(run)
    assert link_names(graph) == ["Init", "Fill", "Fill", "Fill"], (
        "the counterexample fills past the limit"
    )
    link = graph[-1]
    assert set(link) == snapshot, "every documented link field is present"
    assert link["Type"] == "action", "an ordinary step is typed as an action"
    assert link["Node"]["state"] == {"level": 3}, "the failing state is level 3"
    assert link["Node"]["failedInvariants"] == {"0": [0]}, "one invariant is named"


@pytest.mark.timeout(120)
def test_deadlock_and_safety_failures_use_different_lines(tmp_path: Path) -> None:
    """A deadlock prints ``DEADLOCK detected``; a safety failure names the invariant."""
    deadlock = check(
        tmp_path / "deadlock",
        Expectation("deadlock.fizz", FAILED, "the deadlock is reported as a failure"),
    )
    assert "DEADLOCK detected" in deadlock.stdout, "the deadlock is named as one"

    check(
        tmp_path / "safety",
        Expectation(
            "safety_failure.fizz",
            FAILED,
            f"{FAILED}. Invariant:  NeverFull names the failing invariant",
        ),
    )


@pytest.mark.timeout(120)
def test_parse_error_writes_no_model_and_exits_one(tmp_path: Path) -> None:
    """An unprefixed assertion fails compilation, so no run directory appears."""
    run = run_model(tmp_path, "liveness_parse_error.fizz")
    assert run.returncode == 1, "a compile failure is the one non-zero exit"
    assert "mismatched input 'assertion' expecting <EOF>" in run.stdout, (
        "the parser names the token it refused"
    )
    assert "Error: Compilation failed" in run.stdout, "the failure is a compile one"
    assert run.output == tmp_path, "a failed compile writes no run directory"


@pytest.mark.timeout(120)
def test_symmetry_reduction_is_on_by_default(tmp_path: Path) -> None:
    """Two symmetric workers in a bag shrink under the default reduction."""
    check(
        tmp_path / "reduced",
        Expectation(
            "symmetry.fizz",
            PASSED,
            "the reduction collapses symmetric states",
            counts=(9, 7, 3),
        ),
    )
    check(
        tmp_path / "unreduced",
        Expectation(
            "symmetry.fizz",
            PASSED,
            "the flag restores the unreduced counts",
            counts=(12, 10, 4),
            options=("--no-symmetry-reduction",),
        ),
    )


@pytest.mark.timeout(120)
def test_truncation_is_silent_and_can_invent_a_pass_or_a_failure(
    tmp_path: Path,
) -> None:
    """A bound that cuts exploration prints no warning of its own.

    At ``max_actions: 2`` the safety model passes although the level-3 state
    it forbids is reachable; at ``max_actions: 6`` the liveness model fails
    with a stutter the bound produced. Each passes at a larger bound, so the
    bound, not the model, decided both verdicts.
    """
    check(
        tmp_path / "cut-safety",
        Expectation(
            "truncation_safety.fizz",
            PASSED,
            "the bound cuts exploration at 3 nodes and the run passes",
            counts=(3, 3, 3),
        ),
    )
    check(
        tmp_path / "full-safety",
        Expectation(
            "truncation_safety_reaches.fizz",
            FAILED,
            f"without the bound {FAILED}. Invariant:  NeverFull",
        ),
    )
    cut_live = check(
        tmp_path / "cut-live",
        Expectation(
            "truncation_liveness.fizz",
            FAILED_LIVENESS,
            "the bound manufactures a stutter",
        ),
    )
    assert link_names(error_graph(cut_live))[-1] == "stutter", (
        "the manufactured counterexample ends in a stutter"
    )
    check(
        tmp_path / "full-live",
        Expectation(
            "truncation_liveness_reaches.fizz",
            PASSED,
            "without the bound liveness passes",
        ),
    )


@pytest.mark.timeout(120)
def test_two_runs_of_one_model_agree(tmp_path: Path) -> None:
    """The same model and bound give the same graph and the same counts.

    Exploration order is deterministic in v0.5.4, so three runs at
    ``GOMAXPROCS=2`` agree on the masked output and on the graph's structure.
    Only the pointer-style node ids in ``graph.dot`` differ, and the error
    graph does not.
    """
    outputs = set()
    graphs = set()
    for index in range(3):
        run = run_model(tmp_path / str(index), "safety_failure.fizz")
        assert run.returncode == 0, "the safety model compiles"
        outputs.add(run.stdout)
        payload = (run.output / "error-graph.json").read_text(encoding="utf-8")
        graphs.add(payload)
    assert len(outputs) == 1, "the masked output varies between runs"
    assert len(graphs) == 1, "the error graph varies between runs"
