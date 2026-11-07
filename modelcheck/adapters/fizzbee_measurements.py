"""Read the scalars a checker run printed: state counts and durations.

The checker prints its evidence as prose: ``Valid Nodes: 4 Unique states: 4``,
``Nodes: 900, queued: 0``, ``Time taken for model checking: 812.5ms``. This
module turns those lines into the two numbers the domain carries — how much of
the state space a run explored, and how long it took — and nothing else. The
verdict classification, which is a statement about what the run *proved*,
belongs to :mod:`modelcheck.adapters.fizzbee_output`.

The two are separate because they fail differently. A missing state count is a
run that explored nothing and must still be reported; a missing duration is a
recording that masked its timing and must not stop a report. Keeping the
readers apart makes each default explicit at the place it is chosen.

Both readers are total: every input, including the empty string and a run that
printed nothing recognizable, produces a number. That is deliberate. A null
reading is the only sensible answer for evidence the run did not print, and
raising here would turn a real counterexample into a tool failure.
"""

import re

__all__ = ["elapsed", "states"]

_UNIQUE_STATES = re.compile(
    r"^Valid Nodes: \d+ Unique states: (?P<count>\d+)$", re.MULTILINE
)
"""The distinct-state count, which only a completed exploration prints."""

_NODES = re.compile(r"^Nodes: (?P<count>\d+)", re.MULTILINE)
"""The explored-node count, printed even by a run that aborts early."""

_TAKEN = re.compile(r"^Time taken for model checking: (?P<text>.+)$", re.MULTILINE)
"""The checker's own wall-clock duration."""

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ns|us|\N{MICRO SIGN}s|ms|s|m|h)")
"""One number-and-unit part of a Go duration, such as ``812.5ms``.

The microsecond unit is written with a named Unicode escape because that is the
character Go's ``time.Duration`` prints and the checker emits. The Greek letter
mu is a different code point and would not match the checker's output; the
escape is how the intent stays legible without a confusable character in the
source.
"""

_SECONDS = {
    "h": 3600.0,
    "m": 60.0,
    "s": 1.0,
    "ms": 1e-3,
    "us": 1e-6,
    "\N{MICRO SIGN}s": 1e-6,
    "ns": 1e-9,
}
"""The length of each unit the checker's durations use."""


def states(stdout: str) -> int:
    r"""Return how many distinct states the run explored.

    Parameters
    ----------
    stdout : str
        The checker's output.

    Returns
    -------
    int
        The unique-state count when the run printed one; otherwise the explored
        node count, which is the closest figure an aborted run gives; otherwise
        zero. The floor reads this value, so it must come from the run rather
        than being assumed.

    Examples
    --------
    >>> states("Nodes: 9, queued: 0\nValid Nodes: 7 Unique states: 4")
    4
    >>> states("Nodes: 9, queued: 0")
    9
    >>> states("")
    0
    """
    unique = _UNIQUE_STATES.search(stdout)
    if unique is not None:
        return int(unique.group("count"))
    nodes = _NODES.search(stdout)
    return int(nodes.group("count")) if nodes is not None else 0


def elapsed(stdout: str) -> float:
    """Return the duration the checker reported for its own run.

    Parameters
    ----------
    stdout : str
        The checker's output.

    Returns
    -------
    float
        The duration in seconds, or ``0.0`` when the run printed none or the
        figure is a capture placeholder rather than a duration. A recording
        masks the duration, and a recording is what most tests read.

    Examples
    --------
    >>> elapsed("Time taken for model checking: 1.5s")
    1.5
    >>> elapsed("Time taken for model checking: 2m30s")
    150.0
    >>> elapsed("Time taken for model checking: <duration>")
    0.0
    """
    taken = _TAKEN.search(stdout)
    return 0.0 if taken is None else _duration(taken.group("text"))


def _duration(text: str) -> float:
    """Return a Go duration string as seconds.

    Parameters
    ----------
    text : str
        A duration such as ``812.5ms``, ``1.5s`` or ``1m30s``.

    Returns
    -------
    float
        The duration in seconds, or ``0.0`` when the text is not entirely a
        duration, so a placeholder contributes nothing.

    Examples
    --------
    >>> _duration("812.5ms")
    0.8125
    >>> _duration("not a duration")
    0.0
    """
    stripped = text.strip()
    parts = _DURATION_PART.findall(stripped)
    if not parts or _DURATION_PART.sub("", stripped):
        return 0.0
    return sum(float(value) * _SECONDS[unit] for value, unit in parts)
