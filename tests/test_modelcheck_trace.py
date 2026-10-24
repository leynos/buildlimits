"""A normalized trace keeps the path, and marks how it ends.

The checker's ``error-graph.json`` is a list of links whose first entry is the
initial state, whose middle entries are actions, and whose last entry may be a
stutter that names no action at all. A reader of a report wants the actions and
the states they reached, in order, plus a statement of how the path ends — and
a replay wants the index at which a lasso's cycle begins, or the state index a
stutter repeats.

The tests are parametrized over the captured fixtures rather than over
handwritten link lists, so they pin the checker's real format. Two of them are
non-vacuity controls the plan requires: the ``lasso-prefix`` trace must find a
cycle start that is not the trace's first state, and the ``stutter`` trace's
last state must repeat. Without those, a normalizer that assumed the degenerate
shapes would pass everything else here.

Reading a fixture needs no checker, so these tests run in ``make test``
unlike the capture tests in ``tests/test_fizzbee_fixtures.py``.

Every test here was written before its module was implemented (EP-M2), so it
pins a contract the stub could not satisfy.
"""

import typing as typ

import pytest
from fizzbee_spike import captured_graph

if typ.TYPE_CHECKING:
    import collections.abc as cabc

CAPTURED = ("safety-failure", "lasso", "lasso-prefix", "stutter", "deadlock")
"""The classes whose error graph a trace can be normalized from.

Truncation and parse-error runs write no graph, or a graph that does not
describe a counterexample.
"""


def _steps_of(
    links: cabc.Sequence[cabc.Mapping[str, typ.Any]],
) -> list[tuple[str, dict[str, typ.Any]]]:
    """Return the actions and reached states a link list records.

    Parameters
    ----------
    links : Sequence[Mapping[str, Any]]
        The error graph's links.

    Returns
    -------
    list[tuple[str, dict[str, Any]]]
        One pair per link whose type is an action, in order.
    """
    return [
        (str(link["Name"]), dict(link["Node"]["state"]))
        for link in links
        if link.get("Type") == "action"
    ]


@pytest.mark.parametrize("output_class", CAPTURED)
def test_normalize_trace_keeps_the_actions_and_states(output_class: str) -> None:
    """Every action appears once, in order, with the state it reached.

    The action names and their states are read back from the links
    independently of the normalizer, so this pins the translation rather than
    restating it: a normalizer that reordered, deduplicated or dropped a step
    would differ from the graph it came from.
    """
    from modelcheck import trace

    links = captured_graph(output_class)
    normalized = trace.normalize_trace(links)

    assert normalized.initial == links[0]["Node"]["state"], "the graph's first state"
    assert [(step.action, dict(step.state)) for step in normalized.steps] == (
        _steps_of(links)
    ), "every link after the first is one step, in order, with its state"


@pytest.mark.parametrize("output_class", CAPTURED)
def test_normalize_trace_marks_the_ending(output_class: str) -> None:
    """The ending follows from the links: a repeated state or a finite stop.

    A lasso is a path whose last state equals an earlier state; a stutter is
    the link that names no action; everything else stops at a forbidden state.
    The deadlock class is a finite path too — no action is enabled, so its
    links end after the last real step.
    """
    from modelcheck import trace

    normalized = trace.normalize_trace(captured_graph(output_class))

    expected = {
        "safety-failure": trace.Ending.FINITE,
        "lasso": trace.Ending.LASSO,
        "lasso-prefix": trace.Ending.LASSO,
        "stutter": trace.Ending.STUTTER,
        "deadlock": trace.Ending.FINITE,
    }[output_class]
    assert normalized.ending is expected, f"{output_class} ends as {expected}"


def test_the_degenerate_lasso_cycles_onto_its_first_state() -> None:
    """The ``lasso`` class's cycle starts at index 0, which is the trace start.

    This is the shape an earlier capture could only show, and the reason the
    cycle start cannot be read off an assumption. It is pinned here so the
    ``lasso-prefix`` case below is a contrast rather than the only lasso the
    suite knows.
    """
    from modelcheck import trace

    normalized = trace.normalize_trace(captured_graph("lasso"))

    assert normalized.cycle_start == 0, "the cycle closes onto the first state"
    assert normalized.states[0] == normalized.states[-1], "the last state repeats it"


def test_the_lasso_prefix_cycle_start_differs_from_the_trace_start() -> None:
    """A lasso's cycle begins after its prefix, so the start is not index 0.

    ``liveness_prefix.fizz`` must run ``Open`` before ``Gate`` can alternate,
    so the cycle closes onto the state after ``Open``. A classifier that
    assumed the cycle starts at the trace's first state would mislabel this
    trace — and so would one that labels every repeated final state a stutter,
    which is why the terminal state is checked to be a real step's state and
    the cycle start to lie before it.
    """
    from modelcheck import trace

    normalized = trace.normalize_trace(captured_graph("lasso-prefix"))

    assert normalized.cycle_start == 1, "the cycle starts after Open"
    assert normalized.cycle_start != 0, "the prefix is not part of the cycle"
    assert normalized.states[normalized.cycle_start] == normalized.states[-1], (
        "the cycle closes onto the state it started from"
    )
    assert normalized.terminal is None, "a lasso is not a stutter"


def test_the_stutter_repeats_its_last_state() -> None:
    """A stutter's terminal state is the last state, reached and repeated.

    ``Trace.states`` repeats it so a replay can check that the terminal state
    disables every action by looking at a state the checker reported. The link
    that records the stutter names no action, so it must not become a step:
    the only step here is none at all.
    """
    from modelcheck import trace

    normalized = trace.normalize_trace(captured_graph("stutter"))

    assert normalized.terminal == len(normalized.states) - 1, "the last index"
    assert normalized.states[-1] == normalized.states[-2], "the repetition"
    assert not normalized.steps, "the stutter link is not a step"


def test_the_stutter_link_is_not_an_action() -> None:
    """The stutter link is the ending, not a step.

    Both the initial link and the stutter link carry an empty ``Type``; a
    normalizer that stepped on every link after the first would report a step
    named ``"stutter"`` that no action ever took.
    """
    from modelcheck import trace

    normalized = trace.normalize_trace(captured_graph("stutter"))

    assert "stutter" not in {step.action for step in normalized.steps}, (
        "no step is named for the ending"
    )


def test_normalize_trace_rejects_an_empty_graph() -> None:
    """A graph with no links describes no run and must raise."""
    from modelcheck import trace

    with pytest.raises(ValueError, match="empty"):
        trace.normalize_trace([])


def test_normalize_trace_rejects_a_graph_that_does_not_start_at_init() -> None:
    """The first link is the initial state; anything else is not a trace.

    The parser may be handed a graph from a run the tooling did not write, and
    reading its first action as the initial state would silently shift every
    index a replay depends on.
    """
    from modelcheck import trace

    links = captured_graph("safety-failure")[1:]

    with pytest.raises(ValueError, match="initial"):
        trace.normalize_trace(links)


def test_a_deadlock_graph_normalizes_as_a_finite_path() -> None:
    """A deadlock's graph ends where no action is enabled, with no marker.

    The deadlock class is named in the domain as a counterexample kind but has
    no trace marker of its own: the path simply stops, which is what
    ``Ending.FINITE`` says. The verdict kind comes from the checker's line, not
    from the trace.
    """
    from modelcheck import trace

    normalized = trace.normalize_trace(captured_graph("deadlock"))

    assert normalized.ending is trace.Ending.FINITE, "the path simply stops"
    assert normalized.cycle_start is None, "no cycle to start"
    assert normalized.terminal is None, "nothing repeated"
    assert len(normalized.states) == len(normalized.steps) + 1, (
        "one state per step, plus the initial one"
    )
