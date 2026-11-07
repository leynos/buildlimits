"""A path's ending follows from the states it visits, for every path.

Whether a normalized trace is a lasso depends on whether its last state
repeats an earlier one, which is a fact about the states rather than about the
links' shape. No fixed set of captured fixtures can establish that: a
normalizer could agree with all six classes in
``tests/test_modelcheck_trace.py`` and still misreport a path that revisits a
state at another position, because none of the captures happens to revisit it
there.

The properties below therefore draw paths from strategies instead. The state
space is deliberately tiny, so repetition — and with it the lasso branch — is
common; the expected ending, the cycle index and the states a stutter repeats
are all computed here from the drawn states, independently of the normalizer.
The refusals are checked the same way, with the malformed shape built around a
drawn valid path rather than written out for one example.

Hypothesis's failure output is the counterexample that broke the property, so a
regression here reports the exact link list rather than a sentence about one.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from modelcheck import trace


def _state(n: int) -> dict[str, int]:
    """Build one model state from the single counter the path advances.

    Parameters
    ----------
    n : int
        The counter's value.

    Returns
    -------
    dict[str, int]
        The state a link records.
    """
    return {"n": n}


_STATES = st.builds(_state, st.integers(min_value=0, max_value=2))
"""States the properties draw from, small enough that repetition is common.

A lasso needs the last state to equal an earlier one; a wide state space would
make almost every generated path finite and leave the lasso branch of the
normalizer barely exercised.
"""

_TRANSITIONS = st.sampled_from(("Submit", "Grant", "Release"))
"""The transition names a generated path may choose from."""

_STEPS: st.SearchStrategy[list[tuple[str, dict[str, int]]]] = st.lists(
    st.tuples(_TRANSITIONS, _STATES), max_size=6
)
"""Generated transitions, of any length: one name and reached state each."""

_SOME_STEPS: st.SearchStrategy[list[tuple[str, dict[str, int]]]] = st.lists(
    st.tuples(_TRANSITIONS, _STATES), min_size=1, max_size=6
)
"""Generated transitions with at least one, so a stutter has a state to repeat."""

_SOME_MIDDLE: st.SearchStrategy[list[tuple[str, dict[str, int]]]] = st.lists(
    st.tuples(_TRANSITIONS, _STATES), min_size=2, max_size=6
)
"""Generated transitions with a middle position for a misplaced stutter."""


def _path_links(
    initial: dict[str, int], steps: list[tuple[str, dict[str, int]]]
) -> list[trace.Link]:
    """Turn a drawn path into the links the checker would record.

    The steps alternate between actions and ``oneof`` branches, because both
    move the state and the normalizer treats them alike. A property stated over
    actions alone would pass even if branches were dropped.

    Parameters
    ----------
    initial : dict[str, int]
        The state the path starts in.
    steps : list[tuple[str, dict[str, int]]]
        One transition name and reached state per step.

    Returns
    -------
    list[modelcheck.trace.Link]
        The initial link followed by the transition links, in order.
    """
    transitions = [
        trace.Link(
            trace.LinkKind.CHOICE if index % 2 else trace.LinkKind.ACTION,
            name,
            state,
        )
        for index, (name, state) in enumerate(steps)
    ]
    return [trace.Link(trace.LinkKind.INITIAL, None, initial), *transitions]


def _reached(
    initial: dict[str, int], steps: list[tuple[str, dict[str, int]]]
) -> tuple[dict[str, int], ...]:
    """Return the states a generated path passes through, initial state first.

    Parameters
    ----------
    initial : dict[str, int]
        The state the path starts in.
    steps : list[tuple[str, dict[str, int]]]
        One transition name and reached state per step.

    Returns
    -------
    tuple[dict[str, int], ...]
        The initial state and each reached state, in order.
    """
    return (initial, *(state for _, state in steps))


@given(initial=_STATES, steps=_STEPS)
def test_a_path_normalizes_to_the_steps_it_records(
    initial: dict[str, int], steps: list[tuple[str, dict[str, int]]]
) -> None:
    """Any path becomes one step per transition, and its ending follows the states.

    The repetition and its first index are found here from the state sequence,
    so the expectation is the definition rather than a restatement of the
    normalizer's code.
    """
    normalized = trace.normalize_trace(_path_links(initial, steps))
    reached = _reached(initial, steps)

    assert normalized.initial == initial, "the first link is the initial state"
    assert [(step.action, step.state) for step in normalized.steps] == steps, (
        "one step per transition link, in order, with the state it reached"
    )
    assert normalized.states == reached, "no trailing stutter to repeat a state"
    repeats = [
        index for index, state in enumerate(reached[:-1]) if state == reached[-1]
    ]
    if repeats:
        assert normalized.ending is trace.Ending.LASSO, "the last state repeats"
        assert normalized.cycle_start == repeats[0], "the first state it repeats"
    else:
        assert normalized.ending is trace.Ending.FINITE, "nothing repeats"
        assert normalized.cycle_start is None, "so no cycle begins"


@given(initial=_STATES, steps=_SOME_STEPS)
def test_a_stutter_ending_repeats_the_last_state_reached(
    initial: dict[str, int], steps: list[tuple[str, dict[str, int]]]
) -> None:
    """A trailing stutter marks the ending and repeats the state before it.

    The repetition is part of the path rather than an annotation on it: a
    replay checks that the terminal state is disabled by reading
    :attr:`~modelcheck.trace.Trace.states`, so the state a stutter repeats must
    appear there once more, at the index ``terminal`` names.
    """
    reached = _reached(initial, steps)
    links = [
        *_path_links(initial, steps),
        trace.Link(trace.LinkKind.STUTTER, None, reached[-1]),
    ]

    normalized = trace.normalize_trace(links)

    assert normalized.ending is trace.Ending.STUTTER, "the marker is the ending"
    assert [(step.action, step.state) for step in normalized.steps] == steps, (
        "the stutter is not a step of its own"
    )
    assert normalized.states == (*reached, reached[-1]), "the repeat is in the path"
    assert normalized.terminal == len(reached), "the index the repetition occupies"


@given(initial=_STATES, steps=_SOME_MIDDLE)
def test_a_stutter_before_the_end_is_refused(
    initial: dict[str, int], steps: list[tuple[str, dict[str, int]]]
) -> None:
    """Only the last link may mark an ending, wherever a stutter is inserted.

    A stutter in the middle says the run stopped there, which makes every link
    after it unreadable: the path would have continued from a state no action
    was enabled in. The normalizer refuses the shape rather than trimming to
    the stutter, so a malformed graph is reported instead of silently shortened.
    """
    links = _path_links(initial, steps)
    misplaced = [
        links[0],
        trace.Link(trace.LinkKind.STUTTER, None, links[1].state),
        *links[1:],
    ]

    with pytest.raises(ValueError, match="last link"):
        trace.normalize_trace(misplaced)


@given(initial=_STATES, steps=_SOME_STEPS)
def test_a_stutter_that_repeats_no_state_is_refused(
    initial: dict[str, int], steps: list[tuple[str, dict[str, int]]]
) -> None:
    """A stutter must carry the state the path already reached.

    The checker writes the state it stopped in, so a stutter naming a different
    state describes a path that jumped without a transition. Comparing against
    a counter value one greater guarantees the mismatch, which a drawn state
    could not: three values make an accidental match likely rather than rare.
    """
    reached = _reached(initial, steps)
    elsewhere = _state(reached[-1]["n"] + 1)
    links = [
        *_path_links(initial, steps),
        trace.Link(trace.LinkKind.STUTTER, None, elsewhere),
    ]

    with pytest.raises(ValueError, match="repeats no state"):
        trace.normalize_trace(links)
