"""Keep a bounded, redacted copy of a run's output when its evidence is unusable.

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

What is kept is also deliberately redacted. The record this evidence becomes is
committed, so it must describe the model rather than the machine that checked
it. The checker runs with the caller's environment and can echo any of it, so
the run's own paths and the values of its credential-named variables are
replaced before the text leaves the adapter. The specification and the graph
are values this tooling wrote, but the transcript is not: it is whatever the
checker chose to print.
"""

import re
import tempfile
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = [
    "Redaction",
    "bounded",
    "redaction_for",
    "timeout_detail",
    "unreadable_graph",
    "with_evidence",
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

_LAUNCHER_PLACEHOLDER = "<fizzbee>"
"""What the checker launcher's path is replaced with in evidence.

The launcher is the other path the adapter itself names, and it appears in the
message a failed start produces. Where the binary lives is a property of the
runner, not of the model.
"""

_HOME_PLACEHOLDER = "<home>"
"""What the caller's home directory is replaced with in evidence.

A checker that prints a cache or module path prints one under the home
directory. That path is a runner path even though the adapter did not create
it, and it is the prefix most absolute paths in a transcript begin with.
"""

_TEMP_PLACEHOLDER = "<tmp>"
"""What the caller's temporary directory is replaced with in evidence.

The scratch directory lives under it, so the scratch replacement is applied
first; this catches the paths a checker builds beside its own working
directory.
"""

_HIDDEN_VALUE = "<redacted>"
"""What a credential's value is replaced with in evidence."""

_SECRET_NAME = re.compile(
    r"(?:^|_)(?:token|secret|password|passwd|credential|api_?key|private_?key"
    r"|access_?key|auth|cookie|session)(?:$|_)",
    re.IGNORECASE,
)
"""The variable names that mark their value as a credential.

The adapter cannot tell a secret from any other value, so the name is the only
signal available. The pattern matches a whole underscore-separated word, so
``GITHUB_TOKEN`` and ``AWS_SECRET_ACCESS_KEY`` match while ``AUTHOR`` does not.
"""

_MIN_SECRET_LENGTH = 6
"""The shortest value treated as a credential.

Short values are flags and modes — ``tty``, ``1``, ``true`` — and replacing
them would mangle ordinary words in a transcript without protecting anything.
"""


class Redaction(typ.NamedTuple):
    """The substitutions one run's evidence gets before it is retained.

    Attributes
    ----------
    replacements : tuple[tuple[str, str], ...]
        Needle and placeholder pairs, longest needle first. Ordering matters:
        the scratch directory lives under the home and temporary directories,
        so replacing a shorter needle first would leave the longer path in
        pieces.
    """

    replacements: tuple[tuple[str, str], ...]

    def apply(self, text: str) -> str:
        """Return ``text`` with every needle replaced by its placeholder.

        Parameters
        ----------
        text : str
            The checker's output, a message built from a file it wrote, or the
            error graph itself.

        Returns
        -------
        str
            The text with each needle replaced. A text naming none of them is
            returned unchanged.

        Examples
        --------
        >>> redaction = Redaction((("/home/runner", "<home>"),))
        >>> redaction.apply("reading /home/runner/model.fizz")
        'reading <home>/model.fizz'
        """
        for needle, placeholder in self.replacements:
            text = text.replace(needle, placeholder)
        return text


def redaction_for(
    directory: Path,
    launcher: Path,
    environment: cabc.Mapping[str, str],
) -> Redaction:
    """Return the substitutions one run's evidence needs.

    Parameters
    ----------
    directory : pathlib.Path
        The scratch directory this run owns.
    launcher : pathlib.Path
        The checker launcher the adapter invoked.
    environment : collections.abc.Mapping[str, str]
        The environment the checker was given.

    Returns
    -------
    Redaction
        Replacements for the scratch directory, the launcher, the caller's home
        and temporary directories, and every value in ``environment`` whose
        name marks it as a credential. Longest needle first, so a path nested
        inside another is replaced whole.

    Notes
    -----
    The home and temporary directories are read from ``environment`` rather
    than from this process, because that mapping is what the checker was
    actually given. A caller that overrode ``HOME`` for the run would otherwise
    have its override left in the evidence.

    Examples
    --------
    >>> from pathlib import Path
    >>> redaction = redaction_for(
    ...     Path(".model-check/run-1"),
    ...     Path("/opt/fizz"),
    ...     {"HOME": "/home/runner", "GITHUB_TOKEN": "ghp_example_value"},
    ... )
    >>> redaction.apply("open /home/runner/x with ghp_example_value")
    'open <home>/x with <redacted>'
    """
    paths = [
        (str(directory), _SCRATCH_PLACEHOLDER),
        (str(directory.resolve()), _SCRATCH_PLACEHOLDER),
        (str(launcher), _LAUNCHER_PLACEHOLDER),
        (str(launcher.resolve()), _LAUNCHER_PLACEHOLDER),
    ]
    for name, placeholder in (
        ("HOME", _HOME_PLACEHOLDER),
        ("TMPDIR", _TEMP_PLACEHOLDER),
    ):
        value = environment.get(name)
        if value:
            paths.append((value, placeholder))
    paths.append((tempfile.gettempdir(), _TEMP_PLACEHOLDER))
    secrets = [(value, _HIDDEN_VALUE) for value in _secret_values(environment)]
    ordered = sorted(paths + secrets, key=lambda pair: len(pair[0]), reverse=True)
    return Redaction(tuple(ordered))


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


def bounded(text: str) -> str:
    """Return ``text`` cut to :data:`_TRANSCRIPT_LIMIT`, saying so when it was cut.

    Parameters
    ----------
    text : str
        One line the parser decided a run's outcome from, such as the
        checker's own ``Error:`` line or the marker a signal left.

    Returns
    -------
    str
        The text unchanged when it fits, otherwise its first
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
    """
    if len(text) <= _TRANSCRIPT_LIMIT:
        return text
    return (
        f"{text[:_TRANSCRIPT_LIMIT]}… "
        f"(first {_TRANSCRIPT_LIMIT} of {len(text)} characters)"
    )


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
        of the run's paths and credentials.

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


def _secret_values(environment: cabc.Mapping[str, str]) -> tuple[str, ...]:
    """Return the values in ``environment`` whose names mark them as credentials.

    Parameters
    ----------
    environment : collections.abc.Mapping[str, str]
        The environment the checker was given.

    Returns
    -------
    tuple[str, ...]
        Each distinct value long enough to be a credential, longest first, so a
        value that contains another is replaced whole.
    """
    values = {
        value
        for name, value in environment.items()
        if _SECRET_NAME.search(name) and len(value) >= _MIN_SECRET_LENGTH
    }
    return tuple(sorted(values, key=len, reverse=True))


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
