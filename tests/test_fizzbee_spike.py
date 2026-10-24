"""Run the FizzBee spike models and assert the behaviour RFC 0001 records.

These tests drive the real checker, so they are opt-in: without
``BUILD_LIMITS_MODEL_CHECK=1`` they skip and the reason names the variable.
Under the opt-in a missing binary is a failure, not a skip, because an
opted-in run that silently checked nothing would be worse than no run.

Each test runs one model through ``tests/fizzbee_spike.py``, which copies the
spec into pytest's scratch directory so neither the compiled model nor the
``out/`` tree ever lands beside a tracked file. ``tests/fizzbee_expectations``
supplies the ``check`` and ``paired`` calls that state what each run must
show.
"""

import json
import typing as typ
from pathlib import Path

import pytest
from fizzbee_expectations import (
    FAILED_LIVENESS,
    PASSED,
    Expectation,
    check,
    invariant,
    paired,
)
from fizzbee_spike import error_graph, graph_states, link_names, link_states, run_model

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

# ``tests/conftest.py`` skips these modules when the opt-in variable is
# absent, so the reason names it once for every present and future model test.
# A plain assignment here would be reported by the DF12 re-export rule, so the
# mark is written as a list the way the systemd modules do it.
pytestmark = [pytest.mark.fizzbee]


# --- OQ1: state values and fairness granularity -----------------------------


@pytest.mark.timeout(120)
def test_collections_survive_as_state(
    tmp_path: Path, snapshot: SnapshotAssertion
) -> None:
    """Records, dicts, sets, bags and lists are accepted and rendered back."""
    run = check(
        tmp_path,
        Expectation(
            "collections.fizz", PASSED, "every collection is accepted as state"
        ),
    )
    # ``graph.dot`` escapes its state labels, and its node ids vary per run.
    # Pinning the distinct states as a sorted set keeps the contract ("each
    # collection kind survives and renders back") free of exploration order.
    graph = (run.output / "graph.dot").read_text(encoding="utf-8")
    assert sorted(set(graph_states(graph))) == snapshot, (
        "every collection kind renders back as JSON"
    )


@pytest.mark.timeout(120)
def test_role_fairness_is_per_action_and_per_instance(tmp_path: Path) -> None:
    """A fair action inside a role makes every instance progress.

    ``roles_fairness.fizz`` gives each worker's ``Tick`` weak fairness and
    passes; ``roles_fairness_unfair.fizz`` removes the modifier and fails, so
    the pair shows the fairness, not the shape of the model, decides the
    verdict.
    """
    unfair = paired(
        tmp_path,
        should_pass=Expectation(
            "roles_fairness.fizz",
            PASSED,
            "per-instance fairness lets every worker tick",
        ),
        should_fail=Expectation(
            "roles_fairness_unfair.fizz",
            FAILED_LIVENESS,
            "without the modifier a worker is starved",
        ),
    )
    assert link_names(error_graph(unfair))[0] == "Init", (
        "the counterexample starts at Init"
    )


@pytest.mark.timeout(120)
def test_fair_oneof_makes_each_choice_progress(tmp_path: Path) -> None:
    """``fair oneof`` over a bag gives per-instance progress too."""
    check(
        tmp_path,
        Expectation(
            "roles_fairness_fair_oneof.fizz",
            PASSED,
            "fair oneof forces each choice to progress; the reduction explores 4"
            " states",
            counts=(8, 8, 4),
        ),
    )


@pytest.mark.timeout(120)
def test_fairness_levels_and_the_compiled_ast(
    tmp_path: Path, snapshot: SnapshotAssertion
) -> None:
    """A plain action is unfair, ``fair`` is weak and ``fair<strong>`` strong."""
    run = run_model(tmp_path, "fairness_levels.fizz")
    assert run.returncode == 0, "the fairness-levels model compiles"
    ast = json.loads((tmp_path / "fairness_levels.json").read_text(encoding="utf-8"))
    levels = {action["name"]: action["fairness"]["level"] for action in ast["actions"]}
    assert levels == snapshot, "each spelling maps to its fairness level"


@pytest.mark.timeout(120)
def test_interleaving_guard_placement(tmp_path: Path) -> None:
    """A test after a read yields; an atomic ``require`` rolls the step back.

    The snapshot model reads the gate into a local and then tests it, which
    lets the scheduler republish the gate in between, and fails. Reading the
    gate in place, or guarding the snapshot with ``require``, passes. So does
    assigning before a ``require``, because these actions are ``atomic`` and a
    failed ``require`` there discards the whole step. The hazard is an ``if``
    after a read, not the read itself.

    The rollback is a property of atomicity, not of ``require``, and
    ``test_interleaving_plain_rollback`` is the negative control that shows
    the difference.
    """
    check(
        tmp_path / "snapshot",
        Expectation(
            "interleaving_snapshot.fizz",
            FAILED_LIVENESS,
            "a test after a read reaches a stutter in 4 nodes",
            counts=(4, 4, 4),
        ),
    )

    for index, model in enumerate((
        "interleaving_in_place.fizz",
        "interleaving_local_require.fizz",
        "interleaving_assign_require.fizz",
    )):
        check(
            tmp_path / str(index),
            Expectation(model, PASSED, f"{model} has no interleaving hazard"),
        )


# --- OQ2: liveness modes, fairness and how failures are reported ------------


@pytest.mark.timeout(120)
def test_interleaving_plain_rollback(tmp_path: Path) -> None:
    """In a plain action a failed ``require`` keeps the earlier writes.

    ``interleaving_plain_rollback.fizz`` sets ``done`` and then requires the
    gate to be open, and fails from the initial state: the assignment to
    ``done`` commits and yields before the test runs, so a closed-gate step
    leaves ``done`` true with ``v`` still zero. The atomic spelling of the
    same body passes, so the pair fixes the rule that an assignment before a
    test needs atomicity, not merely a ``require``.
    """
    run = check(
        tmp_path,
        Expectation(
            "interleaving_plain_rollback.fizz",
            invariant("NeverClosedDelivery"),
            "the closed-gate delivery violates the safety assertion",
        ),
    )
    graph = error_graph(run)
    assert link_names(graph) == ["Init", "Deliver"], "the unsafe step follows Init"
    assert link_states(graph)[-1] == {"done": True, "v": 0}, (
        "the plain action kept its earlier write"
    )


# Two fairness questions, each posed as a pass/fail pair. The first asks what
# weak fairness does when the action is enabled only intermittently, the second
# what it does when the action never disables.
_FAIRNESS_PAIRS = (
    (
        Expectation(
            "strong_fairness.fizz",
            PASSED,
            "strong fairness forces the action when enabled infinitely often",
        ),
        Expectation(
            "weak_fairness.fizz",
            FAILED_LIVENESS,
            "weak fairness does not force a gate-cycled action; the counterexample"
            " closes the gate twice",
            trace=("Init", "Gate", "Gate"),
        ),
    ),
    (
        Expectation(
            "continuously_enabled.fizz",
            PASSED,
            "weak fairness forces a continuously enabled action",
        ),
        Expectation(
            "continuously_enabled_unfair.fizz",
            FAILED_LIVENESS,
            "without fairness the action is starved; the counterexample is a"
            " stutter on the initial state",
            trace=("Init", "stutter"),
        ),
    ),
)


@pytest.mark.timeout(240)
@pytest.mark.parametrize(("should_pass", "should_fail"), _FAIRNESS_PAIRS)
def test_weak_fairness_reaches_only_as_far_as_its_guarantee(
    tmp_path: Path, should_pass: Expectation, should_fail: Expectation
) -> None:
    """Weak fairness ignores a gate-cycled action but forces a sticky one.

    ``B`` in the first pair is enabled only while the gate is open, and the
    gate can always close again, so ``B`` is not continuously enabled and weak
    fairness does not force it; with ``fair<strong>`` on both actions it is
    enabled infinitely often and must run. In the second pair ``B`` is a
    sticky flag that never disables, so weak fairness is enough, and the same
    model with both actions unfair fails on a stutter from the initial state.
    An unbounded counter makes the second property fail at a low
    ``max_actions``, which is a truncation artefact rather than a fairness
    one. Both pairs run inside one test so the contrast is stated once.
    """
    paired(tmp_path, should_pass=should_pass, should_fail=should_fail)


@pytest.mark.timeout(120)
def test_liveness_modes(tmp_path: Path) -> None:
    """``strict`` and ``eventual`` check liveness; ``none`` does not.

    ``liveness_strict.fizz`` reports the lasso, ``liveness_eventual.fizz``
    passes and prints the nondeterministic-checker line, and
    ``liveness_off.fizz`` passes with no ``Checking`` line at all. Any
    unrecognized string behaves like ``none``, so the RFC names the mode.
    """
    strict = check(
        tmp_path / "strict",
        Expectation(
            "liveness_strict.fizz",
            FAILED_LIVENESS,
            "strict liveness checks always eventually BEventually and finds the lasso",
        ),
    )
    assert "Checking always eventually BEventually" in strict.stdout, (
        "the property is the one the model names"
    )

    eventual = check(
        tmp_path / "eventual",
        Expectation(
            "liveness_eventual.fizz",
            PASSED,
            "eventual liveness passes after dispatching to the"
            " nondeterministic checker",
        ),
    )
    assert "Checking strict liveness with nondeterministic checker" in (
        eventual.stdout
    ), "eventual dispatches to the nondeterministic checker"

    off = check(
        tmp_path / "off",
        Expectation("liveness_off.fizz", PASSED, "the off mode reports a pass"),
    )
    assert "Checking always eventually" not in off.stdout, (
        "the off mode checks no liveness property"
    )


@pytest.mark.timeout(120)
def test_lasso_and_stutter_are_reported_differently(tmp_path: Path) -> None:
    """A lasso cycles through actions; a stutter is a self-loop on one state.

    The lasso's error graph ends where it began and its ``Fairness`` values
    are weak; the stutter's second link is named ``stutter`` with fairness 0
    and repeats the initial state.
    """
    lasso = run_model(tmp_path / "lasso", "liveness_strict.fizz")
    names = link_names(error_graph(lasso))
    assert names == ["Init", "Gate", "Gate"], "the lasso cycles through Gate"
    states = link_states(error_graph(lasso))
    assert states[0] == states[-1], "a lasso returns to its start"

    stutter = run_model(tmp_path / "stutter", "liveness_stutter.fizz")
    stutter_graph = error_graph(stutter)
    assert link_names(stutter_graph) == ["Init", "stutter"], (
        "a stutter is named rather than attributed to an action"
    )
    assert stutter_graph[-1]["Fairness"] == 0, "a stutter link is unfair"
    assert link_states(stutter_graph)[0] == link_states(stutter_graph)[-1], (
        "a stutter repeats one state"
    )


# --- OQ2 negative control: a stutter is not always the model's fault --------
# The captured-fixture pins live in ``test_fizzbee_fixtures.py``, next to the
# table of classes they cover.
