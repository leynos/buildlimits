"""Every state key keeps one distinct redacted name across a whole trace.

A one-character credential withholds every string that contains it, so
redaction is not injective on state keys: two variables redact to the same
text. The trace adapter therefore numbers repeats, and it must number them
from the trace rather than from one state, or the same original key would
appear to move between variables from one state to the next.

The unit tests state one collision each. What the adapter promises is
stronger: for every trace, however nested and in whatever order its states
are recorded, every entry survives, each original key keeps one name, and no
two original keys share a name. The property below draws the credential, the
keys, the states and the nesting, so a numbering rule that only works for the
collision somebody wrote down is a counterexample rather than an agreement
between two copies of one example. Keys are drawn from
:data:`~modelcheck.adapters.fizzbee_replacements.WITHHELD`'s own numbered
forms as well, because a genuine key that already looks numbered must not be
overwritten by a repeat.
"""

import collections.abc as cabc
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from modelcheck.adapters.fizzbee_redaction import redaction_for
from modelcheck.adapters.fizzbee_replacements import WITHHELD
from modelcheck.adapters.fizzbee_trace_redaction import redact_trace
from modelcheck.trace import Ending, Step, Trace

_DIRECTORY = Path(".model-check/run")
"""The scratch directory a run owns, which redaction replaces as a runner path."""

_LAUNCHER = Path("/opt/fizz")
"""The checker launcher, replaced on the same reasoning."""

_SUFFIXED = st.sampled_from([f"{WITHHELD} #{number}" for number in range(1, 6)])
"""Genuine keys that look like the adapter's numbered repeats."""


def _observe(original: object, redacted: object, observed: dict[str, set[str]]) -> None:
    """Record the name each original key got, and that no entry was lost.

    Parameters
    ----------
    original : object
        A state or a nested value as the checker recorded it.
    redacted : object
        The same position in the redacted trace.
    observed : dict[str, set[str]]
        Every name each original key has been given so far, accumulated
        across the whole trace.

    Notes
    -----
    Entries pair by position because the adapter yields them in the order the
    original mapping held them. A repeat that overwrote an entry would shorten
    the redacted mapping, and the length assertion fails before the pairing
    could hide it.
    """
    match original, redacted:
        case (cabc.Mapping(), cabc.Mapping()):
            assert len(original) == len(redacted), "an entry did not survive"
            for (key, value), (name, changed) in zip(
                original.items(), redacted.items(), strict=True
            ):
                observed.setdefault(key, set()).add(name)
                _observe(value, changed, observed)
        case (list(), list()):
            assert len(original) == len(redacted), "a list entry did not survive"
            for value, changed in zip(original, redacted, strict=True):
                _observe(value, changed, observed)
        case _:
            return


@given(data=st.data())
def test_every_state_key_keeps_one_distinct_name_across_a_trace(
    data: st.DataObject,
) -> None:
    """One name per original key, and no name shared by two original keys.

    The credential is drawn first and then used in the keys, so every state has
    at least one genuine collision to number. Keys come from
    :data:`_SUFFIXED` too, so the draws also make a numbered repeat collide
    with a key that already carries that number, in either order.
    """
    credential = data.draw(st.characters(blacklist_categories=("Cs",)))
    key = st.one_of(
        _SUFFIXED,
        st.text(
            st.sampled_from([credential, "a", "b", " "]),
            min_size=1,
            max_size=4,
        ),
    )
    value = st.recursive(
        st.one_of(
            st.none(),
            st.booleans(),
            st.integers(),
            st.text(st.sampled_from(["a", "0", " "]), max_size=4),
        ),
        lambda children: st.one_of(
            st.lists(children, max_size=3),
            st.dictionaries(key, children, max_size=3),
        ),
    )
    states = data.draw(
        st.lists(st.dictionaries(key, value, max_size=4), min_size=1, max_size=4)
    )
    initial, *rest = states
    trace = Trace(
        initial,
        tuple(Step(f"step-{index}", state) for index, state in enumerate(rest)),
        Ending.FINITE,
        None,
    )

    redacted = redact_trace(
        trace, redaction_for(_DIRECTORY, _LAUNCHER, {"GITHUB_TOKEN": credential})
    )

    observed: dict[str, set[str]] = {}
    _observe(trace.initial, redacted.initial, observed)
    for original, changed in zip(trace.steps, redacted.steps, strict=True):
        _observe(original.state, changed.state, observed)

    unstable = {key: names for key, names in observed.items() if len(names) != 1}
    assert not unstable, f"a key changed name across the trace: {unstable}"
    names = [next(iter(found)) for found in observed.values()]
    assert len(set(names)) == len(names), "two original keys share one name"
