"""Which matrix judgements a ``--only`` selection reports.

A selection narrows the report, never the run. The matrix is always run in
full, and the caller computes the exit status from every judgement, so a run
given ``--only`` cannot pass while a configuration it omitted failed. That
separation is what lets a maintainer ask about one expectation without turning
the answer into a claim about the model.

The parsing lives here rather than in :mod:`modelcheck.cli` because it is
policy over values — a judgement in, a judgement out — and the command line
module is where the process's arguments and exit status live. Nothing here
opens a file, starts a process or reads the clock.
"""

import typing as typ

from modelcheck import report

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from modelcheck.domain import Judgement

__all__ = ["select"]


def select(
    judgements: cabc.Sequence[Judgement], only: list[str] | None
) -> tuple[Judgement, ...]:
    """Return the judgements a ``--only`` selection keeps.

    The matrix is always run in full, and a selection narrows only what is
    *reported*. The caller computes the exit status from every judgement, not
    from this sequence, so a run given ``--only`` cannot pass while a
    configuration it omitted failed.

    Parameters
    ----------
    judgements : collections.abc.Sequence[Judgement]
        Every judgement the matrix produced.
    only : list[str] | None
        The raw ``CONFIG[:PROPERTY]`` selections, or ``None`` for everything.

    Returns
    -------
    tuple[Judgement, ...]
        The selected judgements, in matrix order.

    Raises
    ------
    ValueError
        If a selection was given and matched nothing. A narrowed report with
        no rows would exit ``0``, which reads as a pass but says only that the
        tool was asked about an expectation the matrix never produced — a
        mistyped configuration or property name, which is a usage error.

    Examples
    --------
    >>> from modelcheck import domain
    >>> judgement = domain.Judgement(
    ...     "head", domain.Holds("S1"), domain.Passed(4, 0.1, ()),
    ...     domain.Verdict.HOLDS, (),
    ... )
    >>> select([judgement], None) == select([judgement], ["head"])
    True
    >>> select([judgement], ["incident-1"])
    Traceback (most recent call last):
        ...
    ValueError: --only selected incident-1, but the matrix reported no such expectation
    """
    selections = _selections(only)
    chosen = tuple(
        judgement for judgement in judgements if _selected(judgement, selections)
    )
    if selections and not chosen:
        named = ", ".join(sorted(only or ()))
        msg = f"--only selected {named}, but the matrix reported no such expectation"
        raise ValueError(msg)
    return chosen


def _selections(only: list[str] | None) -> tuple[tuple[str, str | None], ...]:
    """Split ``CONFIG[:PROPERTY]`` selections into their parts.

    A selection with no colon names every expectation of one configuration; one
    with a colon names a single property or probe of it.

    Parameters
    ----------
    only : list[str] | None
        The selections as the command line gave them.

    Returns
    -------
    tuple[tuple[str, str | None], ...]
        One pair per selection, the property being ``None`` when the whole
        configuration was named.

    Examples
    --------
    >>> _selections(["head", "incident-1:L3"])
    (('head', None), ('incident-1', 'L3'))
    >>> _selections(None)
    ()
    """
    return tuple(
        (configuration, subject or None)
        for selection in only or []
        for configuration, _, subject in (selection.partition(":"),)
    )


def _selected(
    judgement: Judgement, selections: tuple[tuple[str, str | None], ...]
) -> bool:
    """Report whether a judgement is one of the selected expectations.

    Parameters
    ----------
    judgement : Judgement
        The judgement to test.
    selections : tuple[tuple[str, str | None], ...]
        The parsed ``--only`` selections. Empty means everything is selected.

    Returns
    -------
    bool
        Whether the judgement's configuration is named, and its subject too
        when the selection named one.
    """
    if not selections:
        return True
    return any(
        configuration == judgement.configuration
        and (subject is None or subject == report.subject(judgement.expectation))
        for configuration, subject in selections
    )
