"""Each recorded checker run classifies as its own output requires.

This is the second half of obligation T1. ``test_modelcheck_fizzbee_output``
sweeps every *presence combination* of the marker lines, which proves the
classifier fails closed on shapes the checker could print;
this module sweeps the shapes it *did* print, so the enumeration and the
recordings cannot drift apart without one of them failing.

The truncation captures are the reason both halves exist. Both are runs whose
exploration was cut short by ``max_actions``, and one of them must classify as
a pass because that is genuinely what the checker printed. Refusing it is the
floor's job — ``test_a_bound_truncated_run_parses_as_a_pass`` pins that
division — and a parser that tried to infer truncation from the node count
would be second-guessing the checker. See D22 in the ExecPlan.
"""

import typing as typ

import pytest
from modelcheck_doubles import captured, class_of

from modelcheck import domain, expectations
from modelcheck.adapters import fizzbee_output

CAPTURED: typ.Final = {
    "pass": "Passed",
    "truncation-safety": "Passed",
    "safety-failure": "Counterexample",
    "lasso": "Counterexample",
    "lasso-prefix": "Counterexample",
    "stutter": "Counterexample",
    "deadlock": "Counterexample",
    "truncation-liveness": "Counterexample",
    "parse-error": "Errored",
}
"""Every captured class and the result class its own markers require.

Both truncation classes are recorded as the checker reported them.
``truncation-liveness`` is a counterexample because the checker really did print
a liveness failure; that the bound caused it is what the ``max_actions``
fixpoint detects, not something the parser may infer.
"""

KINDS: typ.Final = {
    "lasso": "LASSO",
    "stutter": "STUTTER",
    "safety-failure": "FINITE",
}
"""The captured classes whose counterexample kind the path alone decides."""


@pytest.mark.parametrize("output_class", list(CAPTURED))
def test_every_captured_class_classifies(output_class: str) -> None:
    """Every recorded run classifies as its own markers require.

    A recording and the sweep in the T1 artefact are two statements of the same
    contract. If the checker changes what it prints, one of them fails: the
    sweep because the marker line no longer matches, or this because the
    recording no longer classifies.
    """
    result = fizzbee_output.parse_output(*captured(output_class))

    assert class_of(result) == CAPTURED[output_class], output_class


def test_a_bound_truncated_run_parses_as_a_pass() -> None:
    """The parser is faithful; the floor, not the parser, refuses a cut run.

    ``truncation-safety`` runs at ``max_actions: 2``, explores three states, and
    prints the pass line. The parser must report a pass — inventing a refusal
    from the node count would be the parser contradicting the checker — and
    ``judge`` must then reject that pass against a floor above three states.
    Both halves are asserted here so a reader can see where the division lies.
    """
    configuration = domain.Configuration("head", {}, {}, {}, 10, (domain.Holds("S2"),))

    result = fizzbee_output.parse_output(*captured("truncation-safety"))
    judgement = expectations.judge(
        configuration,
        domain.Holds("S2"),
        domain.MaterializedSpec("head", "", ()),
        result,
    )

    assert isinstance(result, domain.Passed), "the checker said it passed"
    assert result.states < 10, "its three states are below the floor"
    assert judgement.verdict is domain.Verdict.BELOW_FLOOR, "the floor refuses it"
    assert not judgement.is_met, "so the expectation is unmet"


def test_a_pass_carries_the_states_it_explored() -> None:
    """A pass's state count comes from the run, because the floor reads it.

    The floor is the only mechanism that catches a bound-truncated run, so a
    pass that defaulted its count — to zero, or to the node count — would
    disable it. The count is the distinct-state figure on the ``Valid Nodes``
    line, which differs from the node count wherever both are printed.
    """
    result = fizzbee_output.parse_output(
        "Nodes: 9, queued: 0\nValid Nodes: 7 Unique states: 4\n"
        "PASSED: Model checker completed successfully\n",
        0,
        None,
    )

    assert isinstance(result, domain.Passed), "the run passed"
    assert result.states == 4, "the unique-state count, not the node count"


@pytest.mark.parametrize(
    ("output_class", "carried"),
    [("lasso", "Counterexample"), ("pass", "Passed")],
)
def test_every_class_carries_the_states_its_run_explored(
    output_class: str, carried: str
) -> None:
    """A counterexample reports its state count as a pass does.

    D3 requires every run of one configuration to report the same
    ``states_explored``, and that comparison covers counterexamples as well as
    passes, so the count cannot be a default on one branch and real on the
    other. Both classes are swept from their own captures, because the count
    is read from the same ``Valid Nodes`` line either way.
    """
    result = fizzbee_output.parse_output(*captured(output_class))

    assert class_of(result) == carried, output_class
    assert isinstance(result, (domain.Passed, domain.Counterexample)), carried
    assert result.states == 4, "the unique-state count"


def test_a_deadlock_is_its_own_counterexample_kind() -> None:
    """``DEADLOCK detected`` names the kind rather than a property.

    The deadlock capture prints no ``Invariant:`` line, so a parser reading the
    property from that line would report an empty name. The kind is what the
    report needs, and it comes from the marker.
    """
    result = fizzbee_output.parse_output(*captured("deadlock"))

    assert isinstance(result, domain.Counterexample), "a counterexample"
    assert result.kind is domain.CounterexampleKind.DEADLOCK, "named by the marker"


@pytest.mark.parametrize(("output_class", "kind"), list(KINDS.items()))
def test_a_counterexample_keeps_the_kind_its_trace_has(
    output_class: str, kind: str
) -> None:
    """The kind follows the path's shape, not the marker that reported it.

    A safety failure ends at a forbidden state, a lasso closes onto an earlier
    one, and a stutter repeats the last. All three are reported by a ``FAILED``
    line, so a parser that mapped the marker to a kind would report them all as
    one shape and the report could not tell them apart.
    """
    result = fizzbee_output.parse_output(*captured(output_class))

    assert isinstance(result, domain.Counterexample), "a counterexample"
    assert result.kind is domain.CounterexampleKind[kind], output_class


def test_an_uninterpretable_run_names_the_line_it_failed_on() -> None:
    """An errored run's detail quotes the line, so a report can be diagnosed.

    ``Errored`` carries free text, and a bare "unparsable" would leave a reader
    unable to tell a compilation failure from a schema change in the checker's
    output. The detail is the line that made the run uninterpretable.
    """
    result = fizzbee_output.parse_output(
        "Error: Compilation failed\nLogs at somewhere\n", 0, None
    )

    assert isinstance(result, domain.Errored), "a failure to compile"
    assert "Compilation failed" in result.detail, "the line"


def test_an_interrupted_run_is_stopped_not_errored() -> None:
    """``Model checker stopped`` is its own result, not a generic error.

    The checker prints this on a signal, and the distinction matters: a stopped
    run can be retried, while an errored one means the tooling is wrong. A
    parser that folded it into ``Errored`` would report a retryable interruption
    as a defect. No capture carries this line — producing one means signalling a
    live run — so the transcript is the exact line the checker prints.
    """
    result = fizzbee_output.parse_output(
        "Valid Nodes: 4 Unique states: 4\nModel checker stopped\n", 0, None
    )

    assert isinstance(result, domain.Stopped), "an interruption has its own result"
    assert "stopped" in result.detail.lower(), "the line"
