"""A counterexample's path, normalized so it can be replayed and compared.

The checker records a counterexample as a list of links: the initial state,
then one link per recorded step, naming the transition, the state the step
reached and whether the link records an action at all. A step is either an
action or a ``oneof`` choice: the checker records the branch a ``oneof`` took
as its own link, naming a branch such as ``Any:roots="zero"``. Both move the
state, so both are steps; only the link the checker names ``stutter`` marks an
ending.

Deciding which of those shapes a raw link has is the adapter's job, because
the keys naming them are the checker's own;
:mod:`modelcheck.adapters.fizzbee_graph` decodes them into :class:`Link`
values and this module never sees a checker key. What is left here is the
policy that does not change with the checker: which links are steps, how a
path ends, and where a lasso closes.

That shape is an artefact of the checker and is not what a report or a replay
needs. A :class:`Trace` names the initial state, then one :class:`Step` per
transition, and marks how the path ends:

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

Every link after the first is a step except the stutter. A link whose kind is
:attr:`LinkKind.STUTTER` names no transition, so it marks the ending rather
than appearing in :attr:`Trace.steps`; an :attr:`LinkKind.ACTION` and an
:attr:`LinkKind.CHOICE` both do, which is why a :class:`Step`'s name is a
transition label: the action's name, or the branch a choice took.
"""

# ``dataclasses`` evaluates each field's annotation when it builds the class
# (PEP 649 defers them to that point), so the alias has to exist at runtime.
# This suppression is permanent; moving the import under ``TYPE_CHECKING``
# raises ``NameError`` from the class body.
import collections.abc as cabc  # ruff: ignore[typing-only-standard-library-import]
import dataclasses as dc
import enum


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


class LinkKind(enum.StrEnum):
    """What one recorded link of a counterexample path is.

    The adapter decides this while the checker's own keys are still in hand,
    so the normalizer never has to guess from a name or an empty field.

    Attributes
    ----------
    INITIAL : str
        The state the path starts from, which is not a step.
    ACTION : str
        A transition the model took.
    CHOICE : str
        The branch a nondeterministic choice resolved to. It moves the state,
        so it is a step like an action, and it is recorded separately because
        a replay follows a branch rather than calling an action.
    STUTTER : str
        The final link, saying that no action is enabled where the path
        stopped.
    """

    INITIAL = "initial"
    ACTION = "action"
    CHOICE = "choice"
    STUTTER = "stutter"


@dc.dataclass(frozen=True, slots=True)
class Link:
    """One recorded link, with the checker's keys already decoded.

    Attributes
    ----------
    kind : LinkKind
        What the link is: the initial state, an action, a choice's branch, or
        a stutter.
    name : str | None
        The transition's label for an action or a choice, and ``None`` for the
        two links that record no transition.
    state : collections.abc.Mapping[str, object]
        The model state the link reached. The initial link carries the state
        the path starts in; the others carry the state after the step.
    """

    kind: LinkKind
    name: str | None
    state: cabc.Mapping[str, object]


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


def normalize_trace(links: cabc.Sequence[Link]) -> Trace:
    """Translate one error graph's links into a normalized trace.

    Parameters
    ----------
    links : collections.abc.Sequence[Link]
        The graph's links, decoded and in the order the checker wrote them.

    Returns
    -------
    Trace
        The initial state, the actions taken, and the ending the links
        describe.

    Raises
    ------
    ValueError
        If the links are empty, if the first link is not the initial state,
        if a transition link names no transition, if a stutter appears
        anywhere but the end, or if the stutter repeats no state.

    Examples
    --------
    A choice's branch is a step, because it moves the state: here ``Pick``
    chooses ``"a"`` and the trace records both transitions.

    >>> links = [
    ...     Link(LinkKind.INITIAL, None, {"g": 0}),
    ...     Link(LinkKind.ACTION, "Pick", {"g": 0}),
    ...     Link(LinkKind.CHOICE, 'Any:side="a"', {"g": 1}),
    ...     Link(LinkKind.ACTION, "Gate", {"g": 0}),
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
    if links[0].kind is not LinkKind.INITIAL:
        msg = "the first link is not the initial state"
        raise ValueError(msg)
    steps = tuple(
        Step(_transition(link), link.state)
        for link in links[1:]
        if link.kind is not LinkKind.STUTTER
    )
    reached = (links[0].state, *(step.state for step in steps))
    ending, cycle_start = _ending(links, reached)
    return Trace(reached[0], steps, ending, cycle_start)


def _transition(link: Link) -> str:
    """Return the label of one link that records a transition.

    Parameters
    ----------
    link : Link
        A link whose kind is a step, so an action or a choice.

    Returns
    -------
    str
        The transition's label.

    Raises
    ------
    ValueError
        If the link carries no label. A decoder that reached here has already
        refused the malformed shapes that would otherwise index a missing key,
        so this is the last guard rather than the first.
    """
    if link.name is None:
        msg = f"a transition link names no transition: {link!r}"
        raise ValueError(msg)
    return link.name


def _ending(
    links: cabc.Sequence[Link],
    reached: tuple[cabc.Mapping[str, object], ...],
) -> tuple[Ending, int | None]:
    """Classify how a link list ends.

    The stutter link is checked first because it repeats the state before it,
    which is also the shape of a lasso; only the link itself tells them apart.
    A choice link is a step rather than a mark.

    Parameters
    ----------
    links : collections.abc.Sequence[Link]
        The graph's links.
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
    marks = [index for index, link in enumerate(links) if link.kind is LinkKind.STUTTER]
    if not marks:
        return _lasso(reached)
    if marks != [len(links) - 1]:
        msg = "only the last link may mark an ending"
        raise ValueError(msg)
    if reached[-1] != links[-1].state:
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
