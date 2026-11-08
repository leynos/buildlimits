"""Keep a bounded copy of a run's output when its evidence is unusable.

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

The same evidence is also redacted, because the record it becomes is committed
and must describe the model rather than the machine that checked it. That half
of the job lives in :mod:`modelcheck.adapters.fizzbee_redaction`: this module
decides how much of a text is worth keeping, and that one decides what must
never be kept at all.

The two are applied in one order only: redact, then cut. A cut taken first can
split a credential in two, and the half it keeps is a fragment no survival
check can recognize as a credential — the record would carry five characters of
a secret while looking clean. The caller therefore passes its own scrub
callable here, and each bounding function runs it while the text is still
whole.
"""

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = [
    "Scrub",
    "bounded",
    "timeout_detail",
    "unreadable_graph",
    "with_evidence",
]

type Scrub = cabc.Callable[[str], str]
"""What a caller supplies to remove from a text what must not be recorded.

A run's own wiring passes
:meth:`~modelcheck.adapters.fizzbee_replacements.Redaction.retain`, which
replaces every credential it can and withholds a text one survives in. It is a
parameter rather than an import so this module decides only how much is kept,
while the module that owns the needles is the only one that knows them.
"""

_TRANSCRIPT_LIMIT = 4000
"""How many characters of one captured stream are kept."""


def with_evidence(
    reason: str,
    captures: cabc.Iterable[tuple[str, str]],
    scrub: Scrub | None = None,
) -> str:
    r"""Return a diagnostic followed by the output that explains it.

    Parameters
    ----------
    reason : str
        The failure the parser or adapter established, such as the parse error.
    captures : collections.abc.Iterable[tuple[str, str]]
        Label and text for each piece of evidence, in the order it should be
        shown. An empty or whitespace-only capture is omitted rather than
        rendered as an empty section.
    scrub : Scrub | None
        What must be removed from each capture before it is cut, or ``None``
        when nothing is. It runs first: see :func:`bounded`.

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
    >>> with_evidence(
    ...     "no graph",
    ...     [("the checker printed", "token=abcde")],
    ...     lambda text: text.replace("abcde", "<redacted>"),
    ... )
    'no graph\nthe checker printed:\ntoken=<redacted>'
    """
    parts = [reason]
    parts.extend(
        _bounded(label, text, scrub) for label, text in captures if text.strip()
    )
    return "\n".join(parts)


def bounded(text: str, scrub: Scrub | None = None) -> str:
    """Return ``text`` cut to :data:`_TRANSCRIPT_LIMIT`, saying so when it was cut.

    Parameters
    ----------
    text : str
        One line the parser decided a run's outcome from, such as the
        checker's own ``Error:`` line or the marker a signal left.
    scrub : Scrub | None
        What must be removed from ``text`` before it is cut, or ``None`` when
        nothing is. Applied first, because a cut through a credential would
        keep a fragment of it that no survival check can recognize; a text the
        scrub withholds is bounded as the marker it became.

    Returns
    -------
    str
        The text, scrubbed and unchanged when it fits, otherwise its first
        :data:`_TRANSCRIPT_LIMIT` characters followed by an ellipsis and a note
        stating how much of the whole was kept. A deciding line is bounded for
        the same reason a transcript is: the checker writes it, so its length
        is the checker's choice rather than the adapter's.

    Examples
    --------
    >>> bounded("Error: Compilation failed")
    'Error: Compilation failed'
    >>> bounded("x" * 4001).endswith("(first 4000 of 4001 characters)")
    True
    >>> def scrub(text):
    ...     return "<withheld>" if "s3cr3t" in text else text
    >>> bounded("x" * 3996 + "s3cr3t", scrub)
    '<withheld>'
    """
    kept, whole = _cut(text, scrub)
    if whole is None:
        return kept
    return f"{kept}… (first {_TRANSCRIPT_LIMIT} of {whole} characters)"


def _cut(text: str, scrub: Scrub | None) -> tuple[str, int | None]:
    """Return the text scrubbed and cut, and its whole length when it was cut.

    This is the only place the two steps are ordered, so the order cannot be
    got wrong in one caller and right in the other: a cut taken before the
    scrub splits a credential, and the half it keeps is a fragment the survival
    check cannot recognize as a credential. Every bounded detail in the package
    is built from this function.

    Parameters
    ----------
    text : str
        The capture to bound.
    scrub : Scrub | None
        What must be removed before the cut, or ``None`` when nothing is.

    Returns
    -------
    tuple[str, int | None]
        What is kept, and the whole length when something was cut; ``None``
        when nothing was.
    """
    if scrub is not None:
        text = scrub(text)
    if len(text) <= _TRANSCRIPT_LIMIT:
        return text, None
    return text[:_TRANSCRIPT_LIMIT], len(text)


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
    match error:
        case UnicodeDecodeError():
            return "the error graph could not be read: it is not UTF-8 text"
        case _:
            return f"the error graph could not be read: {error.strerror or error}"


def timeout_detail(
    timeout_seconds: float,
    stdout: str,
    scrub: Scrub | None = None,
) -> str:
    r"""Build the detail for a run the adapter stopped.

    Parameters
    ----------
    timeout_seconds : float
        The limit the run exceeded.
    stdout : str
        Everything the checker printed before it was killed, decoded but not
        yet redacted. It is scrubbed before it is cut; see :func:`bounded`.
    scrub : Scrub | None
        What must be removed from ``stdout`` before it is cut, or ``None`` when
        nothing is.

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
    return with_evidence(reason, (("the checker printed", stdout),), scrub)


def _bounded(label: str, text: str, scrub: Scrub | None) -> str:
    """Return one labelled capture, scrubbed and cut to the transcript limit.

    Parameters
    ----------
    label : str
        What the capture is, such as "the checker printed".
    text : str
        The captured text.
    scrub : Scrub | None
        What must be removed from ``text`` before it is cut, or ``None`` when
        nothing is.

    Returns
    -------
    str
        The labelled capture, with an explicit note when it was truncated. The
        scrub runs on the whole capture, so a credential the cut would have
        split is removed rather than half-kept.
    """
    kept, whole = _cut(text, scrub)
    if whole is None:
        return f"{label}:\n{kept}"
    return f"{label} (first {_TRANSCRIPT_LIMIT} of {whole} characters):\n{kept}\n…"
