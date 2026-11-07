"""Apply one run's substitutions, and refuse text one of them could not clean.

A record a run keeps is committed, so it must describe the model rather than
the machine that checked it. Two values carry that work. :class:`Replacement`
is one substitution: the text to find, what to put in its place, and where it
is allowed to match. A long credential may be replaced wherever it occurs, a
short one only where it stands alone, and a one-character one only in a
position that holds a value — a bare character needle would rewrite every state
count. A :class:`Redaction` is a run's ordered set of them, together with the
credential values they were built from, so the result can be checked rather
than trusted.

The check is the fail-closed half. A substitution can miss a value it was meant
to remove, and the case that matters is a short credential inside a longer
token: no rule can tell that from a coincidence without rewriting the text into
nonsense. :meth:`Redaction.retain` therefore returns a stated marker instead of
a text a credential still occurs in. A record that says evidence was withheld
is honest; one that carried the value is not.

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

_TERMINATOR = r"(?![0-9A-Za-z_.])"
"""What may not follow a one-character value in a value position.

``token=0`` is the flag being echoed, while ``=0.5`` and ``=01`` are parts of a
number the model computed. Ending the match at the first of those and not the
others is what keeps the counts intact.
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
    context : str | None
        The fixed text that must immediately precede ``needle``, or ``None``
        when it may occur anywhere. A one-character credential uses it to name
        a value position, after ``=`` or between quotes, rather than every
        digit in the transcript.
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
        """
        if self.context is not None:
            pattern = (
                rf"(?<={re.escape(self.context)})"
                rf"{re.escape(self.needle)}{_TERMINATOR}"
            )
            return re.sub(pattern, lambda _: self.placeholder, text)
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
    """

    replacements: tuple[Replacement, ...]
    secrets: tuple[str, ...] = ()

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
            Whether one of :attr:`secrets` occurs in it. A value a replacement
            could not remove is what makes the text unsafe to record, whatever
            the rule that missed it.

        Examples
        --------
        >>> Redaction((), ("s3cr3t",)).retains_credential("xs3cr3ty")
        True
        """
        return any(secret in text for secret in self.secrets)

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
