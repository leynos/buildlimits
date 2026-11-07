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
it occurs, a short one only where it stands alone, and a one-character value
only where it is a value rather than a count. All three rules are stated beside
the constants that encode them.

Text is not the only place a credential reaches evidence. A checker that writes
its error graph as JSON writes a *spelling* of each value, and there is more
than one: a quote may or may not be escaped, a non-ASCII character may be
written literally or as a Unicode escape, and Go's encoder writes ``<``, ``>``
and ``&`` as six-character escapes no Python writer produces. The graph's words
are redacted as text with every spelling those writers produce, and the values
decoded from it are redacted again by
:func:`modelcheck.adapters.fizzbee_trace_redaction.redact_result`, so neither
the saved trace nor the record can carry the value in a form the text pass did
not anticipate.

The substitution mechanism itself lives in
:mod:`modelcheck.adapters.fizzbee_replacements`, together with the check that
withholds a text a credential survived in. It is re-exported here so a caller
that thinks about *what* must not be recorded has one module to import.

The bounding half of the same job lives in
:mod:`modelcheck.adapters.fizzbee_evidence`; the two are separate because they
answer different questions. This module decides *what must not be recorded*,
and that module decides *how much of what remains is worth recording*.
"""

import json
import re
import tempfile
import typing as typ
from pathlib import Path

from modelcheck.adapters.fizzbee_replacements import (
    HIDDEN_VALUE,
    Redaction,
    Replacement,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

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


_WHOLE_WORD_SECRET_LENGTH = 6
"""The length below which a credential is replaced only as a whole token.

A short value such as ``abcde`` can occur inside an ordinary word, so it is
matched only where it stands alone. That is exactly where a checker prints a
value it was given — after ``NAME=``, inside JSON quotes — while ``abcdef`` is
replaced wherever it occurs.
"""


_MIN_SECRET_LENGTH = 2
"""The length below which a credential is replaced only in a value position.

A one-character value is too common to be a needle on its own: a shell that
sets ``CLAUDE_CODE_SESSION_ATTENDED=0``, or any other credential-named boolean,
would make ``0`` and ``1`` needles. Replacing them rewrites every state count
in the transcript and turns the error graph's numbers into placeholders, which
is malformed JSON rather than redacted evidence — the record would be
destroyed, and the secret such a value could express is one character.

So the rule follows the position rather than the value: a one-character
credential is replaced where a checker echoes a value it was given — after
``=``, or between quotes — and left alone in a count, which is not a value the
checker was handed. A value of two characters is the shortest replaced as a
standalone token wherever it appears.
"""

_VALUE_CONTEXTS = ("=", '"', "'")
"""The text that makes a one-character value a value rather than a count.

A checker echoes an inherited variable as ``NAME=value``, in a quoted JSON
string, or in single quotes in a message. None of those positions holds a
number the model computed, so replacing there cannot rewrite a state count.
"""


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
        name marks it as a credential, together with each spelling of that
        value that differs from it. Longest needle first, so a path nested
        inside another is replaced whole. The credential values of two or more
        characters are carried beside them, so a text a replacement missed is
        withheld rather than recorded.

    Notes
    -----
    The home and temporary directories are read from ``environment`` rather
    than from this process, because that mapping is what the checker was
    actually given. A caller that overrode ``HOME`` for the run would otherwise
    have its override left in the evidence.

    A credential shorter than :data:`_WHOLE_WORD_SECRET_LENGTH` is replaced
    only where it stands alone, so a five-character value printed as
    ``NAME=abcde`` is removed while the same characters inside a longer word
    are kept. A one-character value is replaced only after ``=`` or between
    quotes; see :data:`_MIN_SECRET_LENGTH` and :data:`_VALUE_CONTEXTS`.

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
    >>> redaction.retains_credential("ghp_example_value")
    True
    """
    paths = [
        Replacement(str(directory), _SCRATCH_PLACEHOLDER),
        Replacement(str(directory.resolve()), _SCRATCH_PLACEHOLDER),
        Replacement(str(launcher), _LAUNCHER_PLACEHOLDER),
        Replacement(str(launcher.resolve()), _LAUNCHER_PLACEHOLDER),
    ]
    for value, placeholder in (
        (environment.get("HOME"), _HOME_PLACEHOLDER),
        (environment.get("TMPDIR"), _TEMP_PLACEHOLDER),
        (tempfile.gettempdir(), _TEMP_PLACEHOLDER),
    ):
        if value and len(Path(value).parts) > 1:
            # A root or single-component value would make a needle that
            # rewrites ordinary text — a ``/`` needle replaces every slash —
            # rather than naming a directory the run used.
            paths.append(Replacement(value, placeholder))
    secrets = _secret_values(environment)
    ordered = sorted(
        paths + _secret_replacements(secrets),
        key=lambda replacement: len(replacement.needle),
        reverse=True,
    )
    return Redaction(
        tuple(ordered),
        tuple(value for value in secrets if len(value) >= _MIN_SECRET_LENGTH),
    )


def _secret_replacements(values: cabc.Iterable[str]) -> list[Replacement]:
    r"""Return the replacements for ``values``, the run's credential values.

    Parameters
    ----------
    values : collections.abc.Iterable[str]
        The values in the checker's environment whose names mark them as
        credentials.

    Returns
    -------
    list[Replacement]
        One replacement per credential value, plus one for each spelling of it
        that differs from the value. A checker that writes its graph as JSON
        escapes a quote or a backslash, may or may not escape a non-ASCII
        character, and — if it is Go — escapes ``<``, ``>`` and ``&`` as well;
        every one of those spellings is written by a real encoder, so every one
        is a needle. Replacing only some would leave the same credential in the
        committed record.

        A one-character value gets one replacement per value position instead
        of a needle of its own; see :data:`_MIN_SECRET_LENGTH`.
    """
    replacements: list[Replacement] = []
    for value in values:
        if len(value) < _MIN_SECRET_LENGTH:
            replacements.extend(
                Replacement(value, HIDDEN_VALUE, context=context)
                for context in _VALUE_CONTEXTS
            )
            continue
        whole_word = len(value) < _WHOLE_WORD_SECRET_LENGTH
        replacements.extend(
            Replacement(needle, HIDDEN_VALUE, whole_word)
            for needle in (value, *_escaped_forms(value))
        )
    return replacements


_GO_ESCAPES = {
    "<": r"\u003c",
    ">": r"\u003e",
    "&": r"\u0026",
    "\u2028": r"\u2028",
    "\u2029": r"\u2029",
}
r"""The characters Go's JSON encoder escapes and Python's does not.

``encoding/json`` writes each as a six-character escape so its output is safe
inside an HTML ``<script>`` element. Those spellings exist in the checker's
own graphs, and a credential holding one would survive a Python-shaped needle,
so each is a needle of its own.
"""


def _escaped_forms(value: str) -> tuple[str, ...]:
    r"""Return the JSON spellings of ``value`` that differ from it.

    There are three writers, not one. Python's ``json`` may write a non-ASCII
    character literally or as ``\uXXXX`` and escapes a quote or a backslash; a
    credential mixing an escaped quote with a literal character — ``a\"é`` —
    is neither the value itself nor any single spelling of it, so each form is
    returned. Go's ``encoding/json`` additionally escapes ``<``, ``>``, ``&``
    and the line separators as six-character escapes Python never produces.
    And a diagnostic the adapter builds quotes a decoded value with ``repr``,
    which escapes a backslash but not the quote character it did not choose;
    the parser's refusal is built that way, so its spelling is a needle too.

    Parameters
    ----------
    value : str
        A credential's literal value.

    Returns
    -------
    tuple[str, ...]
        Each spelling an encoder produces for ``value``, without the
        surrounding quotes, that is not the value itself. Sorted so a run's
        needles do not depend on set iteration order.

    Examples
    --------
    >>> _escaped_forms("plain")
    ()
    >>> _escaped_forms('a"b')
    ('a\\"b',)
    >>> _escaped_forms("a<b")
    ('a\\u003cb',)
    >>> len(_escaped_forms('a"é'))
    2
    """
    forms = {
        json.dumps(value, ensure_ascii=ascii_only)[1:-1] for ascii_only in (True, False)
    }
    forms.add(repr(value)[1:-1])
    forms |= {_go_escaped(form) for form in forms}
    return tuple(sorted(form for form in forms if form != value))


def _go_escaped(form: str) -> str:
    r"""Return ``form`` with the characters Go's encoder escapes replaced.

    Parameters
    ----------
    form : str
        One spelling of a credential, without its surrounding quotes.

    Returns
    -------
    str
        The same spelling as Go's ``encoding/json`` would write it. A form
        holding none of those characters is returned unchanged.

    Examples
    --------
    >>> _go_escaped("a<b")
    'a\\u003cb'
    >>> _go_escaped("plain")
    'plain'
    """
    for character, escape in _GO_ESCAPES.items():
        form = form.replace(character, escape)
    return form


def _secret_values(environment: cabc.Mapping[str, str]) -> tuple[str, ...]:
    """Return the values in ``environment`` whose names mark them as credentials.

    Parameters
    ----------
    environment : collections.abc.Mapping[str, str]
        The environment the checker was given.

    Returns
    -------
    tuple[str, ...]
        Each distinct non-empty value, longest first, so a value that contains
        another is replaced whole. A one-character value is included: how it is
        replaced is :func:`_secret_replacements`'s decision rather than this
        one's, and a text is withheld on a surviving value of two or more
        characters only.

    Notes
    -----
    An empty value is excluded: replacing it would insert the placeholder
    between every pair of characters rather than remove anything.
    """
    values = {
        value
        for name, value in environment.items()
        if _SECRET_NAME.search(name) and value
    }
    return tuple(sorted(values, key=len, reverse=True))
