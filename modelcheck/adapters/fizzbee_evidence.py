"""Keep a bounded copy of a run's output when its evidence cannot be used.

A failing run's error graph is the only record of the path it took. When that
graph exists but cannot be parsed or read, the parser can name the failure but
not what the run printed or what it wrote. The process adapter deletes the
scratch directory after every run, so that evidence is gone unless the result
carries it.

What is kept is deliberately bounded. The transcript is a checker dump, and a
report that embedded all of it would be unreadable and would put the whole of
a runaway run's output into the JSON artefact. A fixed prefix is enough to
identify the malformed record or the checker's own complaint; the cut is
stated rather than silent, so a reader knows the evidence was truncated.

The text is the model check's own output, not production data: the
specification and the graph are values this tooling wrote to its scratch
directory.
"""

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ["with_evidence"]

_TRANSCRIPT_LIMIT = 4000
"""How many characters of one captured stream are kept."""


def with_evidence(reason: str, captures: cabc.Iterable[tuple[str, str]]) -> str:
    """Return a diagnostic followed by the output that explains it.

    Parameters
    ----------
    reason : str
        The failure the parser or adapter established, such as the parse error.
    captures : collections.abc.Iterable[tuple[str, str]]
        Label and text for each piece of evidence, in the order it should be
        shown. An empty or whitespace-only capture is omitted rather than
        rendered as an empty section.

    Returns
    -------
    str
        The reason, then each non-empty capture under its label. A capture
        longer than :data:`_TRANSCRIPT_LIMIT` keeps its first part and says how
        much of the whole that is.

    Examples
    --------
    >>> print(with_evidence("no graph", [("the checker printed", "Error: boom")]))
    no graph
    the checker printed:
    Error: boom
    >>> with_evidence("no graph", [("the checker printed", "  ")])
    'no graph'
    """
    parts = [reason]
    parts.extend(_bounded(label, text) for label, text in captures if text.strip())
    return "\n".join(parts)


def _bounded(label: str, text: str) -> str:
    """Return one labelled capture, cut to :data:`_TRANSCRIPT_LIMIT`.

    Parameters
    ----------
    label : str
        What the capture is, such as "the checker printed".
    text : str
        The captured text.

    Returns
    -------
    str
        The labelled capture, with an explicit note when it was truncated.
    """
    if len(text) <= _TRANSCRIPT_LIMIT:
        return f"{label}:\n{text}"
    return (
        f"{label} (first {_TRANSCRIPT_LIMIT} of {len(text)} characters):\n"
        f"{text[:_TRANSCRIPT_LIMIT]}\n…"
    )
