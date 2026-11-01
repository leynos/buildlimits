"""A counterexample's path, normalized so it can be replayed and compared.

The checker's ``error-graph.json`` is a list of links: the initial state, then
one link per recorded step, naming the transition, the state the step reached
and whether that link is an action at all. A step is either an action or a
``oneof`` choice: the checker records the branch a ``oneof`` took as its own
link, with an empty ``Type`` and a name such as ``Any:roots="zero"``. Both move
the state, so both are steps; only the link the checker names ``stutter`` marks
an ending. That shape is an artefact of the checker and is not what a report or
a replay needs. A :class:`Trace` names the initial state, then one
:class:`Step` per transition, and marks how the path ends:

- a finite path simply stops at its last state;
- a lasso closes a cycle, and the cycle's start is found by comparing states
  rather than assumed to be the trace's first step (D15), because a prefix may
  have to run before the cycle can begin;
- a stutter ends where nothing more happens, so its terminal state is the
  last state reached, repeated.

Both marks are indices into :attr:`Trace.states`, where index ``0`` is the
initial state and index ``k`` is the state after step ``k - 1``. A replay can
therefore name the step a disposition refers to without re-deriving it, and the
report can say how many steps the cycle had.

Every link after the first is a step except the stutter. The stutter names no
action (its ``Type`` is empty, like the initial link's), so it marks the ending
rather than appearing in :attr:`Trace.steps`; a ``oneof`` link has the same
empty ``Type``, and is told apart by its name. A ``Step``'s name is therefore a
transition label: the action's name, or the branch a choice took.
"""

import dataclasses as dc
import enum
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class Ending(enum.StrEnum):
    """How a normalized counterexample path ends.

    Attributes
    ----------
    FINITE : str
        The path stops at a forbidden state.
    LASSO : str
        The path's last state repeats an earlier one and the cycle is taken
        for ever.
    STUTTER : str
        The path ends in a state where nothing more happens.
    """

    FINITE = "finite"
    LASSO = "lasso"
    STUTTER = "stutter"


@dc.dataclass(frozen=True, slots=True)
class Step:
    """One action taken, and the model state the action reached.

    Attributes
    ----------
    action : str
        The transition's label, as the checker recorded it: an action's name,
        or the branch a ``oneof`` took.
    state : collections.abc.Mapping[str, object]
        The model state after the action ran.
    """

    action: str
    state: cabc.Mapping[str, object]


@dc.dataclass(frozen=True, slots=True)
class Trace:
    """A normalized counterexample path, with its ending marked.

    Attributes
    ----------
    initial : collections.abc.Mapping[str, object]
        The model state before the first action.
    steps : tuple[Step, ...]
        The actions taken, in the order the checker took them.
    ending : Ending
        How the path ends.
    cycle_start : int | None
        For a lasso, the index in :attr:`states` of the state the last state
        repeats. ``None`` for every other ending.
    """

    initial: cabc.Mapping[str, object]
    steps: tuple[Step, ...]
    ending: Ending
    cycle_start: int | None

    @property
    def states(self) -> tuple[cabc.Mapping[str, object], ...]:
        """Every state in the path, initial state first.

        A stutter repeats its last state, so the repetition is part of the
        sequence rather than an annotation on it: a replay that checks the
        terminal state is disabled is checking a state the checker reported.

        Returns
        -------
        tuple[collections.abc.Mapping[str, object], ...]
            The initial state, the state each step reached, and, for a
            stutter, that last state once more.

        Examples
        --------
        >>> trace = Trace({}, (), Ending.STUTTER, None)
        >>> trace.states
        ({}, {})
        """
        reached = (self.initial, *(step.state for step in self.steps))
        if self.ending is Ending.STUTTER:
            return (*reached, reached[-1])
        return reached

    @property
    def terminal(self) -> int | None:
        """The state index a stutter ends in, or ``None``.

        Returns
        -------
        int | None
            The index of the repeated state for a stutter ending, and
            ``None`` for a finite path or a lasso, whose last state is the
            state a step reached rather than a repetition.

        Examples
        --------
        >>> Trace({}, (), Ending.FINITE, None).terminal is None
        True
        """
        return len(self.states) - 1 if self.ending is Ending.STUTTER else None


def normalize_trace(
    links: cabc.Sequence[cabc.Mapping[str, object]],
) -> Trace:
    """Translate one error graph's links into a normalized trace.

    Parameters
    ----------
    links : collections.abc.Sequence[collections.abc.Mapping[str, object]]
        The ``error-graph.json`` links, in the order the checker wrote them.

    Returns
    -------
    Trace
        The initial state, the actions taken, and the ending the links
        describe.

    Raises
    ------
    ValueError
        If the links are empty, if the first link is not the initial state,
        if a stutter appears anywhere but the end, or if a lasso's or stutter's
        mark cannot be located.

    Examples
    --------
    A ``oneof`` link is a step, because it moves the state: here ``Pick``
    chooses ``"a"`` and the trace records both transitions.

    >>> links = [
    ...     {"Type": "", "Name": "Init", "Node": {"state": {"g": 0}}},
    ...     {"Type": "action", "Name": "Pick", "Node": {"state": {"g": 0}}},
    ...     {"Type": "", "Name": 'Any:side="a"', "Node": {"state": {"g": 1}}},
    ...     {"Type": "action", "Name": "Gate", "Node": {"state": {"g": 0}}},
    ... ]
    >>> trace = normalize_trace(links)
    >>> [(step.action, dict(step.state)) for step in trace.steps]
    [('Pick', {'g': 0}), ('Any:side="a"', {'g': 1}), ('Gate', {'g': 0})]
    >>> trace.ending, trace.cycle_start
    (<Ending.LASSO: 'lasso'>, 0)
    """
    if not links:
        msg = "an empty error graph describes no run"
        raise ValueError(msg)
    if links[0].get("Type"):
        msg = "the first link is not the initial state"
        raise ValueError(msg)
    steps = tuple(_step(link) for link in links[1:] if not _marks_the_stutter(link))
    reached = (_state(links[0]), *(step.state for step in steps))
    ending, cycle_start = _ending(links, reached)
    return Trace(reached[0], steps, ending, cycle_start)


def _is_action(link: cabc.Mapping[str, object]) -> bool:
    """Report whether one link records an action.

    Parameters
    ----------
    link : collections.abc.Mapping[str, object]
        One entry of ``error-graph.json``.

    Returns
    -------
    bool
        Whether the link's ``Type`` names an action. The initial link, a
        ``oneof`` link and the stutter link all leave it empty.
    """
    return link.get("Type") == "action"


def _marks_the_stutter(link: cabc.Mapping[str, object]) -> bool:
    """Report whether one link is the ending rather than a transition.

    The checker writes the stutter as a link that names no action. A ``oneof``
    link also names no action, so the ``Type`` alone cannot separate them: the
    stutter is the one the checker names ``stutter``, which no model can shadow
    because an action of that name would carry an action ``Type``.

    Parameters
    ----------
    link : collections.abc.Mapping[str, object]
        One entry of ``error-graph.json``.

    Returns
    -------
    bool
        Whether the link marks a stutter ending.
    """
    return not _is_action(link) and link.get("Name") == "stutter"


def _state(link: cabc.Mapping[str, object]) -> cabc.Mapping[str, object]:
    """Return the model state one link records.

    Parameters
    ----------
    link : collections.abc.Mapping[str, object]
        One entry of ``error-graph.json``.

    Returns
    -------
    collections.abc.Mapping[str, object]
        The state under ``Node.state``.
    """
    node = typ.cast("cabc.Mapping[str, object]", link["Node"])
    return typ.cast("cabc.Mapping[str, object]", node["state"])


def _step(link: cabc.Mapping[str, object]) -> Step:
    """Build one step from an action link.

    Parameters
    ----------
    link : collections.abc.Mapping[str, object]
        A link whose ``Type`` is ``action``.

    Returns
    -------
    Step
        The action's name and the state it reached.
    """
    return Step(str(link["Name"]), _state(link))


def _ending(
    links: cabc.Sequence[cabc.Mapping[str, object]],
    reached: tuple[cabc.Mapping[str, object], ...],
) -> tuple[Ending, int | None]:
    """Classify how a link list ends.

    The stutter mark is checked first because it repeats the state before it,
    which is also the shape of a lasso; only the mark itself tells them apart.
    A link that names no action and is not the stutter is a ``oneof`` choice,
    which is a step rather than a mark.

    Parameters
    ----------
    links : collections.abc.Sequence[collections.abc.Mapping[str, object]]
        The error graph's links.
    reached : tuple[collections.abc.Mapping[str, object], ...]
        The initial state and the state each action reached, in order.

    Returns
    -------
    tuple[Ending, int | None]
        The ending, and for a lasso the index the cycle closes onto.

    Raises
    ------
    ValueError
        If a stutter appears anywhere but the end, or if the stutter repeats
        no state.
    """
    marks = [
        index
        for index, link in enumerate(links[1:], start=1)
        if _marks_the_stutter(link)
    ]
    if not marks:
        return _lasso(reached)
    if marks != [len(links) - 1]:
        msg = "only the last link may mark an ending"
        raise ValueError(msg)
    if reached[-1] != _state(links[-1]):
        msg = "the stutter's link repeats no state"
        raise ValueError(msg)
    return Ending.STUTTER, None


def _lasso(
    reached: tuple[cabc.Mapping[str, object], ...],
) -> tuple[Ending, int | None]:
    """Classify a path whose links all name actions.

    Parameters
    ----------
    reached : tuple[collections.abc.Mapping[str, object], ...]
        The initial state and the state each action reached, in order.

    Returns
    -------
    tuple[Ending, int | None]
        ``LASSO`` and the first index the last state repeats, or ``FINITE``
        and ``None`` when it repeats no state.
    """
    last = reached[-1]
    for index, state in enumerate(reached[:-1]):
        if state == last:
            return Ending.LASSO, index
    return Ending.FINITE, None
