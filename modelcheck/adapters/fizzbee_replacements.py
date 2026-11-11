"""Apply one run's substitutions, and refuse text one of them could not clean.

A record a run keeps is committed, so it must describe the model rather than
the machine that checked it. Two values carry that work. :class:`Replacement`
is one substitution: the text to find, what to put in its place, and where it
is allowed to match. A long credential may be replaced wherever it occurs, a
short one only where it stands alone, and a one-character one only in a
position that holds a value — after an assignment, between quotes, or after a
credential's own label — because a bare character needle would rewrite every
state count. A :class:`Redaction` is a run's ordered set of them, together with
the credential values they were built from, so the result can be checked rather
than trusted.

The check is the fail-closed half. A substitution can miss a value it was meant
to remove, and the case that matters is a short credential inside a longer
token: no rule can tell that from a coincidence without rewriting the text into
nonsense. :meth:`Redaction.retain` therefore returns a stated marker instead of
a text a credential of two or more characters still occurs in. A one-character
credential is outside that search, because it occurs in every count: a
diagnostic that embeds one keeps it, and that residual is stated rather than
hidden. A record that says evidence was withheld is honest; one that carried a
value the rules can see is not.

Which needles exist, and why each one is needed, belongs to
:mod:`modelcheck.adapters.fizzbee_redaction`. This module holds only the
mechanism, so each file answers one question.
"""

import re
import typing as typ

__all__ = ["HIDDEN_VALUE", "WITHHELD", "Redaction", "Replacement"]

HIDDEN_VALUE = "<redacted>"
"""What a credential's value is replaced with in evidence."""

WITHHELD = "<withheld: a credential could not be removed>"
"""What a text is replaced with when a credential survives substitution.

The marker names the reason rather than the value, so a reader knows the
evidence exists and was not recorded, and the record still says why.
"""

_TERMINATOR = r"(?![0-9A-Za-z_]|\.[0-9])"
"""What may not follow a one-character value in a value position.

``token=0`` is the flag being echoed, while ``=0.5`` and ``=01`` are parts of a
number the model computed. Ending the match at the first of those and not the
others is what keeps the counts intact.

A full stop blocks the match only when a digit follows it, because that is the
spelling of a fraction. A full stop ending the sentence — ``token=0.`` — is
punctuation after a value, and refusing to match there would leave the
credential in the text.
"""

_JSON_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})|\\([\"\\/bfnrt])")
"""A JSON escape inside a string, as a writer may spell one."""

_ESCAPED_CHARACTERS = {
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


def _decoded_view(text: str) -> str:
    r"""Return ``text`` with JSON's escapes resolved, for the survival check.

    A credential can be written in more spellings than any list of needles
    enumerates: the value ``a<b`` may appear with its less-than sign written as
    a Unicode escape, with every character so written, or as any mixture of the
    two. The survival check therefore looks at the text both as it was written
    and as a JSON reader would decode it, so a spelling nobody anticipated
    withholds the text rather than being recorded.

    Parameters
    ----------
    text : str
        A text already passed through :meth:`Redaction.apply`.

    Returns
    -------
    str
        The text with every JSON escape resolved and each surrogate pair
        combined into the character it encodes. A sequence that is not an
        escape is left as it is.
    """

    def resolve(match: re.Match[str]) -> str:
        """Return the character the matched escape stands for.

        Returns
        -------
        str
            The one character the escape encodes.
        """
        if match.group(1) is not None:
            return chr(int(match.group(1), 16))
        return _ESCAPED_CHARACTERS[match.group(2)]

    decoded = _JSON_ESCAPE.sub(resolve, text)
    # A writer may split a non-BMP character into two \u escapes. Encoding and
    # decoding again joins that pair, so a credential containing one is still
    # found; a lone surrogate, which is not a character, becomes the
    # replacement character in this view and cannot match a real secret.
    return decoded.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


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
    context : str | None
        A regular expression that must immediately precede ``needle``, or
        ``None`` when it may occur anywhere. A one-character credential uses it
        to name a position that holds a value — after ``=`` or a quote, or after
        the label a checker prints beside it — rather than every digit in the
        transcript. The matched context is kept, so only the value disappears.
    """

    needle: str
    placeholder: str
    whole_word: bool = False
    context: str | None = None

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
        >>> Replacement("0", "<redacted>", context="=").apply(
        ...     "token=0 of 10, ratio=0.5"
        ... )
        'token=<redacted> of 10, ratio=0.5'
        >>> Replacement("x", "<redacted>", context=r"(?i:token|session) ").apply(
        ...     "token x beside token xyz"
        ... )
        'token <redacted> beside token xyz'
        """
        if self.context is not None:
            pattern = (
                rf"(?P<context>{self.context})"
                rf"{re.escape(self.needle)}{_TERMINATOR}"
            )
            return re.sub(
                pattern,
                lambda match: match.group("context") + self.placeholder,
                text,
            )
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
    secrets : tuple[str, ...]
        The credential values the replacements were built from, omitting those
        of one character. A single character occurs in every count, so finding
        one says nothing about whether it was echoed; the values of two or more
        characters are the ones a surviving occurrence is evidence of.
    character_secrets : tuple[str, ...]
        The credential values of exactly one character. A text is not withheld
        on one of those, because every transcript on a host that holds a
        one-character boolean would then be withheld. A decoded string is
        different: it is a single value the checker produced, so a character
        inside it cannot be told from the text it was built from, and the
        string is withheld rather than recorded.
    """

    replacements: tuple[Replacement, ...]
    secrets: tuple[str, ...] = ()
    character_secrets: tuple[str, ...] = ()

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

    def retains_credential(self, text: str) -> bool:
        """Return whether any credential still occurs in ``text``.

        Parameters
        ----------
        text : str
            A text already passed through :meth:`apply`.

        Returns
        -------
        bool
            Whether one of :attr:`secrets` occurs in it, as written or as a
            JSON reader decodes it. A value a replacement could not remove is
            what makes the text unsafe to record, whatever the rule that missed
            it, and a spelling no needle enumerated is still the same value.

        Examples
        --------
        >>> Redaction((), ("s3cr3t",)).retains_credential("xs3cr3ty")
        True
        """
        decoded = _decoded_view(text)
        return any(
            secret in candidate
            for candidate in (text, decoded)
            for secret in self.secrets
        )

    def retain(self, text: str) -> str:
        """Return ``text`` redacted, or the marker withholding it.

        Parameters
        ----------
        text : str
            A text the caller intends to keep as evidence: the checker's
            transcript, an error's detail, or one string of a decoded trace.

        Returns
        -------
        str
            The text with each replacement made, or :data:`WITHHELD` when a
            credential still occurs in it afterwards. Withholding is the only
            answer that is both honest and safe: the rule that missed the value
            cannot be tightened without rewriting text that is not a secret.

        Examples
        --------
        >>> redaction = Redaction(
        ...     (Replacement("abcde", HIDDEN_VALUE, whole_word=True),), ("abcde",)
        ... )
        >>> redaction.retain("token=abcde")
        'token=<redacted>'
        >>> redaction.retain("token=xabcdey")
        '<withheld: a credential could not be removed>'
        """
        redacted = self.apply(text)
        return WITHHELD if self.retains_credential(redacted) else redacted

    def retain_value(self, value: str) -> str:
        """Return one decoded JSON string redacted, or the withholding marker.

        A decoded string is a value in its own right, so a one-character
        credential needs no surrounding ``=`` or quote to be recognized: the
        whole string is the position it would occupy in the text. Without this,
        a state value that is exactly a credential's one character would
        survive the text rule and reach the saved trace.

        A string that merely *contains* the character is withheld. The text
        rule cannot see the character there, and a checker that built a state
        from an inherited value would leave it recoverable; withholding the one
        string keeps that from being recorded while the rest of the trace
        survives.

        Parameters
        ----------
        value : str
            One string decoded from the checker's JSON.

        Returns
        -------
        str
            The placeholder when the whole string is a credential, otherwise
            the redacted string, or the marker when one could not be cleaned.

        Examples
        --------
        >>> redaction = Redaction((Replacement("0", HIDDEN_VALUE, context="="),))
        >>> redaction.retain_value("0")
        '<redacted>'
        """
        for replacement in self.replacements:
            if replacement.needle == value:
                return replacement.placeholder
        redacted = self.apply(value)
        if self.retains_credential(redacted) or any(
            secret in redacted for secret in self.character_secrets
        ):
            return WITHHELD
        return redacted
