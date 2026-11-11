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
between two copies of one example.

The draws are shaped so the interesting cases cannot be skipped. Every trace
has at least two states, every state holds every key, and the states are
permutations of one another, so a name that followed a position rather than a
key would be caught. At least two keys contain the credential, so every trace
has a genuine collision to number. Keys are drawn from
:data:`~modelcheck.adapters.fizzbee_replacements.WITHHELD`'s own numbered
forms as well, because a genuine key that already looks numbered must not be
overwritten by a repeat. One state is nested inside a list and a mapping, and
the observer fails if redaction changes a container's type rather than
silently treating it as a leaf.
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

_CREDENTIALS = st.sampled_from(["a", "b", "#", "<", " ", "0"])
"""One-character credentials, including ones that appear in ``WITHHELD``.

A credential drawn from a fixed set keeps the shrinker away from character
intervals: Hypothesis's ``characters`` strategy is drawn through ``st.data``
here, and shrinking one of its intervals has failed inside the shrinker itself
rather than reporting the property's own assertion.
"""


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

    Raises
    ------
    AssertionError
        If an entry was lost, or if redaction turned a mapping or a list into
        a value of another type. The second check matters because a container
        that became a leaf would otherwise be compared as one and its entries
        would never be observed.

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
        case (cabc.Mapping() | list(), _):
            raise AssertionError("redaction changed a container's type")
        case _:
            return


def _colliding_keys(data: st.DataObject, credential: str) -> list[str]:
    """Draw at least two distinct keys that the credential withholds.

    Parameters
    ----------
    data : hypothesis.strategies.DataObject
        The draw source, so the keys can depend on the credential.
    credential : str
        The one-character credential the run's environment carries.

    Returns
    -------
    list[str]
        Two or three distinct keys, each the credential followed by at least
        one more character, so each is withheld rather than replaced as a
        whole-string credential and the adapter must number the repeats.
    """
    return data.draw(
        st.lists(
            st.text(
                st.sampled_from([credential, "a", "b"]), min_size=1, max_size=3
            ).map(lambda tail: credential + tail),
            min_size=2,
            max_size=3,
            unique=True,
        )
    )


def _value(key: st.SearchStrategy[str]) -> st.SearchStrategy[object]:
    """Draw a state value, nested to a bounded depth.

    Parameters
    ----------
    key : hypothesis.strategies.SearchStrategy[str]
        The strategy for keys of a nested mapping.

    Returns
    -------
    hypothesis.strategies.SearchStrategy[object]
        A leaf, or a non-empty list or mapping of further values.
    """
    return st.recursive(
        st.one_of(
            st.none(),
            st.booleans(),
            st.integers(),
            st.text(st.sampled_from(["a", "0", " "]), max_size=4),
        ),
        lambda children: st.one_of(
            st.lists(children, min_size=1, max_size=3),
            st.dictionaries(key, children, min_size=1, max_size=3),
        ),
    )


def _drawn_trace(data: st.DataObject, credential: str) -> Trace:
    """Draw a trace whose states are distinct permutations of one key set.

    Parameters
    ----------
    data : hypothesis.strategies.DataObject
        The draw source.
    credential : str
        The one-character credential the run's environment carries.

    Returns
    -------
    modelcheck.trace.Trace
        Two to four states, each holding every drawn key, with one state
        nested inside a list and a mapping.
    """
    colliding = _colliding_keys(data, credential)
    suffixed = data.draw(st.lists(_SUFFIXED, min_size=1, max_size=2, unique=True))
    pool = colliding + suffixed
    key = st.sampled_from(pool)
    value = _value(key)

    first = data.draw(st.permutations(pool))
    others = data.draw(
        st.lists(
            st.permutations(pool).filter(lambda order: tuple(order) != tuple(first)),
            min_size=1,
            max_size=3,
        )
    )
    orders = [first, *others]
    states = [{name: data.draw(value) for name in order} for order in orders]
    nested_state = data.draw(st.integers(min_value=0, max_value=len(states) - 1))
    nested_key = data.draw(st.sampled_from(orders[nested_state]))
    states[nested_state][nested_key] = [{"inner": states[nested_state][nested_key]}]

    initial, *rest = states
    return Trace(
        initial,
        tuple(Step(f"step-{index}", state) for index, state in enumerate(rest)),
        Ending.FINITE,
        None,
    )


@given(data=st.data())
def test_every_state_key_keeps_one_distinct_name_across_a_trace(
    data: st.DataObject,
) -> None:
    """One name per original key, and no name shared by two original keys.

    The credential is drawn first and then used in at least two keys, so every
    state has a genuine collision to number. At least one key comes from
    :data:`_SUFFIXED`, so the draws also make a numbered repeat collide with a
    key that already carries that number, in either order. The states are
    distinct permutations of the same key set, so a name that followed a
    position rather than a key is a counterexample. One state is always nested
    inside a list and a mapping.
    """
    credential = data.draw(_CREDENTIALS)
    trace = _drawn_trace(data, credential)
    redacted = redact_trace(
        trace, redaction_for(_DIRECTORY, _LAUNCHER, {"GITHUB_TOKEN": credential})
    )

    observed: dict[str, set[str]] = {}
    _observe(trace.initial, redacted.initial, observed)
    for original, changed in zip(trace.steps, redacted.steps, strict=True):
        _observe(original.state, changed.state, observed)

    unstable = {name: names for name, names in observed.items() if len(names) != 1}
    assert not unstable, f"a key changed name across the trace: {unstable}"
    names = [next(iter(found)) for found in observed.values()]
    assert len(set(names)) == len(names), "two original keys share one name"
