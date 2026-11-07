"""No spelling of a credential survives redaction, for every generated value.

The unit tests state one spelling each: a value as it was written, the form a
Python JSON writer escapes, Go's six-character escapes, and a value mixing an
escaped quote with a literal non-ASCII character. A fixed set of examples can
only say that those spellings are handled. What the redaction promises is
stronger — a committed record does not reveal the credential in *any* spelling
a writer produces — and that is a claim about every value, not about the ones
somebody thought to write down.

The properties below therefore draw the credential, the text around it and the
spelling from strategies, and compute the expected spellings here rather than
importing the adapter's own list, so a needle the implementation forgot is a
counterexample rather than an agreement between two copies of one list.

Withholding is an acceptable outcome: the contract is that the evidence either
does not carry the credential or says it was withheld, never that it carries it
quietly. The examples that demand the placeholder itself live with the unit and
end-to-end tests, where the exact text is part of what a reader is promised.
"""

import json
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
