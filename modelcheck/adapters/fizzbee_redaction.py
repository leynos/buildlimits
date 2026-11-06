"""Replace what describes the runner before a run's text becomes evidence.

The record a failed run keeps is committed, so it must describe the model
rather than the machine that checked it. The checker runs with the caller's
environment and can echo any of it: its own launcher path, the scratch
directory it works in, a cache path under the caller's home, and the value of
any credential-named variable it inherited. Each of those is a fact about the
runner, and each is replaced here before the text leaves the adapter.

The adapter cannot tell a secret from any other value, so a variable's name is
the only signal available. That makes the length of a value the hard part of
the problem rather than an afterthought: a long value can be replaced wherever
it occurs, while a short one can occur inside an ordinary word. Both rules —
and the one residual they leave — are stated beside the constants that encode
them.

The bounding half of the same job lives in
:mod:`modelcheck.adapters.fizzbee_evidence`; the two are separate because they
answer different questions. This module decides *what must not be recorded*,
and that module decides *how much of what remains is worth recording*.
"""

import json
import re
import tempfile
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = ["Redaction", "Replacement", "redaction_for"]

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

_MIN_SECRET_LENGTH = 2
"""The shortest value treated as a credential.

A one-character value is not replaced, and that is a deliberate residual. It is
indistinguishable from a state count, a flag or an exit status: ``Unique states:
1`` is the very evidence a record exists to carry, so replacing every ``1`` in a
transcript would destroy the report without protecting a secret it can no
longer express. Two characters is the shortest value a reader could recognize
as a credential at all.
"""

_WHOLE_WORD_SECRET_LENGTH = 6
"""The length below which a credential is replaced only as a whole token.

A short value such as ``abcde`` can occur inside an ordinary word, so it is
matched only where it stands alone. That is exactly where a checker prints a
value it was given — after ``NAME=``, inside JSON quotes — while ``abcdef`` is
left intact.
"""


class Replacement(typ.NamedTuple):
    """One substitution applied to a run's evidence.

    Attributes
    ----------
    needle : str
        The text to replace.
    placeholder : str
        What ``needle`` is replaced with.
    whole_word : bool
        Whether ``needle`` is replaced only where it stands alone. A short
        credential is a value like ``abcde``, and replacing it wherever it
        occurs would rewrite the middle of ordinary words.
    """

    needle: str
    placeholder: str
    whole_word: bool = False

    def apply(self, text: str) -> str:
        """Return ``text`` with this replacement made.

        Parameters
        ----------
        text : str
            The checker's output, a message built from a file it wrote, or the
            error graph itself.

        Returns
        -------
        str
            The text with the needle replaced. A text naming none of it is
            returned unchanged.

        Examples
        --------
        >>> Replacement("/home/runner", "<home>").apply("in /home/runner/x")
        'in <home>/x'
        >>> Replacement("abcde", "<redacted>", whole_word=True).apply(
        ...     "token=abcde beside abcdef"
        ... )
        'token=<redacted> beside abcdef'
        """
        if not self.whole_word:
            return text.replace(self.needle, self.placeholder)
        # A replacement string is not a template, so it is produced by a
        # callable rather than parsed for backslash escapes the placeholder
        # never contains.
        pattern = rf"(?<![0-9A-Za-z_]){re.escape(self.needle)}(?![0-9A-Za-z_])"
        return re.sub(pattern, lambda _: self.placeholder, text)


class Redaction(typ.NamedTuple):
    """The substitutions one run's evidence gets before it is retained.

    Attributes
    ----------
    replacements : tuple[Replacement, ...]
        The substitutions, longest needle first. Ordering matters: the scratch
        directory lives under the home and temporary directories, so replacing
        a shorter needle first would leave the longer path in pieces.
    """

    replacements: tuple[Replacement, ...]

    def apply(self, text: str) -> str:
        """Return ``text`` with every replacement made.

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
        >>> redaction = Redaction((Replacement("/home/runner", "<home>"),))
        >>> redaction.apply("reading /home/runner/model.fizz")
        'reading <home>/model.fizz'
        """
        for replacement in self.replacements:
            text = replacement.apply(text)
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
        name marks it as a credential, together with that value's JSON-escaped
        form. Longest needle first, so a path nested inside another is replaced
        whole.

    Notes
    -----
    The home and temporary directories are read from ``environment`` rather
    than from this process, because that mapping is what the checker was
    actually given. A caller that overrode ``HOME`` for the run would otherwise
    have its override left in the evidence.

    A credential shorter than :data:`_WHOLE_WORD_SECRET_LENGTH` is replaced
    only where it stands alone, so a five-character value is still removed
    without rewriting the middle of an ordinary word. A one-character value is
    not replaced at all; see :data:`_MIN_SECRET_LENGTH` for why that residual
    is accepted.

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
        Replacement(str(directory), _SCRATCH_PLACEHOLDER),
        Replacement(str(directory.resolve()), _SCRATCH_PLACEHOLDER),
        Replacement(str(launcher), _LAUNCHER_PLACEHOLDER),
        Replacement(str(launcher.resolve()), _LAUNCHER_PLACEHOLDER),
    ]
    for name, placeholder in (
        ("HOME", _HOME_PLACEHOLDER),
        ("TMPDIR", _TEMP_PLACEHOLDER),
    ):
        value = environment.get(name)
        if value:
            paths.append(Replacement(value, placeholder))
    paths.append(Replacement(tempfile.gettempdir(), _TEMP_PLACEHOLDER))
    ordered = sorted(
        paths + _secret_replacements(environment),
        key=lambda replacement: len(replacement.needle),
        reverse=True,
    )
    return Redaction(tuple(ordered))


def _secret_replacements(
    environment: cabc.Mapping[str, str],
) -> list[Replacement]:
    r"""Return the replacements for the credentials in ``environment``.

    Parameters
    ----------
    environment : collections.abc.Mapping[str, str]
        The environment the checker was given.

    Returns
    -------
    list[Replacement]
        One replacement per credential value, plus one for its JSON-escaped
        form when the two differ. A checker that writes its graph as JSON
        escapes a quote or a backslash in the value it writes, so the document
        holds ``a\\"b`` where the credential is ``a"b``; replacing only the
        literal value would leave the escaped form — the same credential — in
        the committed record.
    """
    replacements = []
    for value in _secret_values(environment):
        whole_word = len(value) < _WHOLE_WORD_SECRET_LENGTH
        replacements.append(Replacement(value, _HIDDEN_VALUE, whole_word))
        escaped = json.dumps(value)[1:-1]
        if escaped != value:
            replacements.append(Replacement(escaped, _HIDDEN_VALUE, whole_word))
    return replacements


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
