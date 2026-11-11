"""No spelling of a credential survives redaction, for every generated value.

The unit tests state one spelling each: a value as it was written, the form a
Python JSON writer escapes, Go's six-character escapes, and a value mixing an
escaped quote with a literal non-ASCII character. A fixed set of examples can
only say that those spellings are handled. What the redaction promises is
stronger — a committed record does not reveal the credential in *any* spelling
a writer produces — and that is a claim about every value, not about the ones
somebody thought to write down.

The credentials drawn here are two to six characters long, the lengths the text
rule searches for. A one-character credential is covered separately: the text
rule is positional, so a character embedded in free prose is the accepted
residual, while a decoded string containing one is withheld by its own property
below.

The properties below therefore draw the credential, the text around it and the
spelling from strategies, and compute the expected spellings here rather than
importing the adapter's own list, so a needle the implementation forgot is a
counterexample rather than an agreement between two copies of one list.

One property goes further and spells each character independently — as itself
or as a six-character escape — because a writer may mix the two within one
value, and no list of whole-value spellings enumerates that. It then decodes
what was retained with a decoder of its own and looks for the credential, so
the check is about what the record *means*, not about which strings it holds.

Withholding is an acceptable outcome: the contract is that the evidence either
does not carry the credential or says it was withheld, never that it carries it
quietly. The examples that demand the placeholder itself live with the unit and
end-to-end tests, where the exact text is part of what a reader is promised.
"""

import json
import re
import typing as typ
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from modelcheck.adapters.fizzbee_redaction import redaction_for
from modelcheck.adapters.fizzbee_replacements import WITHHELD

if typ.TYPE_CHECKING:
    from modelcheck.adapters.fizzbee_replacements import Redaction

_DIRECTORY = Path(".model-check/run")
"""The scratch directory a run owns, which redaction replaces as a runner path."""

_LAUNCHER = Path("/opt/fizz")
"""The checker launcher, replaced on the same reasoning."""

_CHARACTERS = st.sampled_from([
    "a",
    "b",
    "0",
    '"',
    "\\",
    "<",
    ">",
    "&",
    "é",
    "\u2028",
    "/",
    " ",
])
"""The characters a generated credential is built from.

They are the ones whose spellings differ between writers: a quote and a
backslash are escaped by every JSON writer, a less-than sign and the line
separators only by Go's, and a non-ASCII character may be written either way.
A plain letter is included so the property also covers the trivial case.
"""

_CREDENTIALS = st.text(_CHARACTERS, min_size=2, max_size=6)
"""Generated credentials, of the lengths a value is replaced as a token."""

_SURROUNDING = st.text(st.sampled_from("abc "), max_size=4)
"""Text around the credential, drawn from characters the placeholders use."""

_GO_ESCAPES = {
    "<": r"\u003c",
    ">": r"\u003e",
    "&": r"\u0026",
    "\u2028": r"\u2028",
    "\u2029": r"\u2029",
}
r"""The characters Go's ``encoding/json`` escapes and Python's does not."""


def _spellings(credential: str) -> set[str]:
    r"""Return every JSON spelling a writer may produce for ``credential``.

    Parameters
    ----------
    credential : str
        The value a checker was given and may print.

    Returns
    -------
    set[str]
        The value as Python writes it with and without ASCII-only escaping,
        and each of those as Go's encoder writes it.

    Examples
    --------
    >>> sorted(_spellings('a"é'))
    ['a\\"\\u00e9', 'a\\"é']
    """
    written = {
        json.dumps(credential, ensure_ascii=ascii_only)[1:-1]
        for ascii_only in (True, False)
    }
    return written | {
        "".join(_GO_ESCAPES.get(character, character) for character in form)
        for form in written
    }


def _escaped(character: str, *, as_escape: bool) -> str:
    r"""Return one character as a JSON writer may spell it inside a string.

    Parameters
    ----------
    character : str
        One character of a generated credential.
    as_escape : bool
        Whether to write it as a six-character Unicode escape rather than as
        itself. A quote or a backslash is escaped either way; no writer may
        emit those literally.

    Returns
    -------
    str
        The character's spelling, without the quotes that would surround a
        whole JSON string.
    """
    if character in '"\\':
        return json.dumps(character)[1:-1]
    if as_escape:
        return f"\\u{ord(character):04x}"
    return json.dumps(character, ensure_ascii=False)[1:-1]


_UNESCAPED = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
"""What each single-character JSON escape stands for."""

_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})|\\([\"\\/bfnrt])")
r"""A JSON escape sequence, as either a Unicode escape or a named one.

The named alternative lists JSON's eight escapes rather than accepting any
character after a backslash. That is what the format says, and it matters for
this test: redaction can cut an escape in half — a credential of ``000000`` in
``000000`` leaves ``\u003<redacted>`` — and an incomplete ``\u`` is not an
escape, so a decoder must leave it as the text it is rather than fail on it.
"""


def _decoded(text: str) -> str:
    """Return ``text`` with every JSON escape resolved, for the assertion.

    This is deliberately a decoder of the test's own. Reusing the adapter's
    would let one mistake in it satisfy both the redaction and the check that
    the redaction worked.

    Parameters
    ----------
    text : str
        Retained evidence, as the redaction returned it.

    Returns
    -------
    str
        The text a JSON reader would see.
    """

    def resolve(match: re.Match[str]) -> str:
        """Return the character one matched escape stands for.

        Returns
        -------
        str
            The character the escape encodes.
        """
        if match.group(1) is not None:
            return chr(int(match.group(1), 16))
        return _UNESCAPED[match.group(2)]

    return _ESCAPE.sub(resolve, text)


def _redaction_with(credential: str) -> Redaction:
    """Return the redaction a run whose environment holds ``credential`` gets.

    Parameters
    ----------
    credential : str
        The value of a credential-named variable the checker inherited.

    Returns
    -------
    modelcheck.adapters.fizzbee_replacements.Redaction
        The substitutions that run's evidence is passed through.
    """
    return redaction_for(_DIRECTORY, _LAUNCHER, {"GITHUB_TOKEN": credential})


@given(credential=_CREDENTIALS, before=_SURROUNDING, after=_SURROUNDING)
def test_no_spelling_of_a_credential_survives_redaction(
    credential: str, before: str, after: str
) -> None:
    """Every spelling of a credential is removed, or the text is withheld.

    The spellings are computed here rather than read from the implementation,
    so a writer's form the needles miss shows up as a counterexample. The
    literal value is checked too: a writer need not escape anything, and the
    record must not carry the credential even as it was given.
    """
    redaction = _redaction_with(credential)

    for spelling in _spellings(credential) | {credential}:
        text = f"{before} {spelling} {after}"
        retained = redaction.retain(text)

        assert retained == WITHHELD or not any(
            candidate in retained for candidate in (spelling, credential)
        ), f"{spelling!r} survived in {retained!r}"


@st.composite
def _mixed_escape_spelling(draw: st.DrawFn) -> tuple[str, str]:
    r"""Return a credential and one spelling that escapes its characters singly.

    The escape pattern is drawn after the credential and with the credential's
    length, so every draw is usable. Drawing a fixed-length pattern instead and
    filtering on a matching length would discard most draws, and Hypothesis
    rightly reports that as a health check failure.

    Parameters
    ----------
    draw : hypothesis.strategies.DrawFn
        The strategy's draw function.

    Returns
    -------
    tuple[str, str]
        The credential, and a spelling of it in which each character is written
        either as itself or as a six-character escape, decided per character.
    """
    credential = draw(_CREDENTIALS)
    escapes = draw(
        st.lists(st.booleans(), min_size=len(credential), max_size=len(credential))
    )
    spelling = "".join(
        _escaped(character, as_escape=escape)
        for character, escape in zip(credential, escapes, strict=True)
    )
    return credential, spelling


@given(
    spelling_and_credential=_mixed_escape_spelling(),
    before=_SURROUNDING,
    after=_SURROUNDING,
)
def test_a_mixed_escape_spelling_does_not_decode_to_the_credential(
    spelling_and_credential: tuple[str, str], before: str, after: str
) -> None:
    """A spelling that escapes each character independently is still removed.

    A writer may mix literal and escaped characters within one value, so the
    spelling a record carries need not be any whole-value form a needle list
    enumerates. The assertion therefore decodes what was retained and looks for
    the credential there: evidence that *means* the credential is not safe,
    whatever bytes it is made of.
    """
    credential, spelling = spelling_and_credential

    retained = _redaction_with(credential).retain(f"{before} {spelling} {after}")

    assert retained == WITHHELD or credential not in _decoded(retained), (
        f"{credential!r} decoded from {retained!r}"
    )


@given(character=st.characters(blacklist_categories=("Cs",)))
def test_a_decoded_string_containing_any_one_character_credential_is_withheld(
    character: str,
) -> None:
    """A decoded string is withheld on any one-character credential.

    The text rule is positional, because a bare character occurs in every
    count. A decoded string is different: it is one value the checker produced,
    so a character inside it cannot be told from the text it was built from,
    and the string is withheld whatever the character is. Drawing the character
    from the whole space keeps that from depending on the ones a test listed.
    """
    redaction = _redaction_with(character)

    retained = redaction.retain_value(f"prefix{character}suffix")

    assert retained == WITHHELD, f"{character!r} survived in {retained!r}"
