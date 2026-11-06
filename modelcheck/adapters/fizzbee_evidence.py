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
    from pathlib import Path

__all__ = [
    "redacted",
    "timeout_detail",
    "unreadable_graph",
    "with_evidence",
    "without_scratch",
]

_TRANSCRIPT_LIMIT = 4000
"""How many characters of one captured stream are kept."""

_SCRATCH_PLACEHOLDER = "<scratch>"
"""What the run's own scratch directory is replaced with in evidence.

The directory's absolute path is a property of this machine and this run. It
identifies nothing about the model, and a record that carried it would leak a
runner path into an artefact a maintainer commits. The placeholder keeps the
part of a message that matters — that something under the scratch directory
could not be read — without the part that only makes sense on the host that
ran it.
"""


def redacted(text: str, directory: Path) -> str:
    """Return a run's output with its scratch directory path replaced.

    Parameters
    ----------
    text : str
        The checker's output, or a message built from a file under the scratch
        directory.
    directory : pathlib.Path
        The directory this run owns, which is the only runner path the adapter
        knows it created.

    Returns
    -------
    str
        The text with every occurrence of the directory's absolute path
        replaced by :data:`_SCRATCH_PLACEHOLDER`. Both the path as given and
        its resolved form are replaced, because the adapter may have been
        handed a relative path while a diagnostic carries the absolute one.

    Examples
    --------
    >>> from pathlib import Path
    >>> scratch = Path(".model-check/run-1")
    >>> redacted(f"open {scratch}/model.fizz failed", scratch)
    'open <scratch>/model.fizz failed'
    """
    needles = {str(directory), str(directory.resolve())}
    for needle in sorted(needles, key=len, reverse=True):
        text = text.replace(needle, _SCRATCH_PLACEHOLDER)
    return text


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


def without_scratch(graph: str | None, directory: Path) -> str | None:
    """Return an error graph with this run's scratch directory replaced.

    Parameters
    ----------
    graph : str | None
        The graph's text, or ``None`` when the run wrote none.
    directory : pathlib.Path
        The scratch directory this run owns.

    Returns
    -------
    str | None
        The graph with the runner path replaced, or ``None`` unchanged. A graph
        that names no path is returned as it was, so the ordinary case is not
        rewritten.

    Examples
    --------
    >>> from pathlib import Path
    >>> without_scratch(None, Path(".model-check/run-1")) is None
    True
    >>> graph = '{"state": {"p": ".model-check/run-1"}}'
    >>> without_scratch(graph, Path(".model-check/run-1"))
    '{"state": {"p": "<scratch>"}}'
    """
    if graph is None:
        return None
    return redacted(graph, directory)


def unreadable_graph(error: OSError | UnicodeDecodeError) -> str:
    r"""Return the reason a graph that exists could not be read.

    Parameters
    ----------
    error : OSError | UnicodeDecodeError
        The failure the read raised.

    Returns
    -------
    str
        The reason, naming the file and the operating system's own message.
        The absolute path is deliberately not included: it is a runner path,
        and the record it becomes is committed evidence about the model.

    Examples
    --------
    >>> unreadable_graph(IsADirectoryError(21, "Is a directory"))
    'the error graph could not be read: Is a directory'
    >>> unreadable_graph(UnicodeDecodeError("utf-8", b"\\xff", 0, 1, "invalid"))
    'the error graph could not be read: it is not UTF-8 text'
    """
    if isinstance(error, UnicodeDecodeError):
        return "the error graph could not be read: it is not UTF-8 text"
    return f"the error graph could not be read: {error.strerror or error}"


def timeout_detail(timeout_seconds: float, stdout: str) -> str:
    r"""Build the detail for a run the adapter stopped.

    Parameters
    ----------
    timeout_seconds : float
        The limit the run exceeded.
    stdout : str
        Everything the checker printed before it was killed, already redacted
        of the scratch directory's path.

    Returns
    -------
    str
        The reason, followed by a bounded copy of the checker's own output when
        there was any. Keeping it is the difference between a report naming the
        state count the model had reached and a report saying only that time
        ran out; bounding it keeps a runaway run's transcript out of the
        artefact.

    Examples
    --------
    >>> timeout_detail(600.0, "Nodes: 41\n").splitlines()[0]
    'the checker did not finish within 600s'
    >>> timeout_detail(600.0, "").splitlines()
    ['the checker did not finish within 600s']
    """
    reason = f"the checker did not finish within {timeout_seconds:.0f}s"
    return with_evidence(reason, (("the checker printed", stdout),))


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
