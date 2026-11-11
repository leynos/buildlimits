"""Read the assertion names a checker run printed.

The checker names an assertion in two places: on the line that reports a
violation, and on the ``Invariant N: <name>`` lines that follow a liveness
failure or a never-reached marker. Both are text the checker wrote, so reading
them is a parsing job rather than a decision about the run; the verdict itself
stays in :mod:`modelcheck.adapters.fizzbee_output`.

This module is pure. It reads text already in hand and never starts the checker
or touches the filesystem.
"""

import re

__all__ = ["following", "name", "probes"]

_INVARIANT = re.compile(r"^Invariant(?:\s+\d+)?:\s*(?P<name>.+)$")
"""An assertion name, either ``Invariant: X`` or ``Invariant 0: X``."""


def name(line: str) -> str:
    """Return the assertion name carried on one line.

    Parameters
    ----------
    line : str
        A line such as ``FAILED: Model checker failed. Invariant:  NeverFull``.

    Returns
    -------
    str
        The name with the marker and surrounding space removed.

    Examples
    --------
    >>> name("FAILED: Model checker failed. Invariant:  NeverFull")
    'NeverFull'
    """
    _, _, tail = line.partition("Invariant:")
    return tail.strip()


def following(lines: list[str], index: int) -> str:
    """Return the assertion name printed after a liveness failure.

    Parameters
    ----------
    lines : list[str]
        The checker's output lines.
    index : int
        The index of the ``FAILED: Liveness check failed`` line.

    Returns
    -------
    str
        The name on the next ``Invariant:`` line, or the empty string when the
        run printed none, so a report still shows the finding without a name it
        never had.
    """
    for line in lines[index + 1 :]:
        match = _INVARIANT.match(line.strip())
        if match:
            return match.group("name").strip()
    return ""


def probes(lines: list[str], marker: str) -> tuple[str, ...]:
    """Return the ``exists`` assertions a completed run never witnessed.

    Parameters
    ----------
    lines : list[str]
        The checker's output lines.
    marker : str
        The line that opens the list, which the caller owns because it is the
        same marker the caller classified the run by.

    Returns
    -------
    tuple[str, ...]
        Each name printed on an ``Invariant N: <name>`` line after the marker,
        in the checker's own order.
    """
    start = next(index for index, line in enumerate(lines) if line.startswith(marker))
    return tuple(
        match.group("name").strip()
        for line in lines[start + 1 :]
        if (match := _INVARIANT.match(line.strip()))
    )
