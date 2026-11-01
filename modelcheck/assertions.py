"""Read, require and strip the assertions a specification declares.

A configuration states what its run should show in terms of the model's
assertions and ``exists`` probes. Those names are the tooling's only link
between a report row and the text FizzBee compiles, so both directions are
checked here rather than trusted.

Forwards, every expectation a configuration names has to be declared. A
``Holds`` or ``Reachable`` naming an assertion the model no longer carries is
never looked up by the checker, so the run passes the remaining specification
and the judgement reports the absent property as holding — the run would prove
something other than what the report claims.

Backwards, a mutant keeps only the assertions its run is about, because D3
gives each run exactly one expected failure. A name retained by that strip that
matches no declaration would leave a specification asserting nothing while its
report named expectations, and the checker would report a pass.

The assertions are found by their headers, whatever temporal prefix they carry,
so a property and an ``exists`` probe are read by the same rule.
"""

import re
import typing as typ

from modelcheck.domain import FailsAs, Holds, MaterializeError

if typ.TYPE_CHECKING:
    from modelcheck.domain import Configuration

_ASSERTION = re.compile(
    r"^(?:(?:always[ \t]+eventually|always|exists)[ \t]+)?"
    r"assertion[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_]*):"
)
"""An assertion's header, in any of the three temporal prefixes."""


def require_declared(lines: list[str], configuration: Configuration) -> None:
    """Refuse a configuration expecting an assertion the model does not declare.

    Every expectation is checked here, whether the run keeps it or strips it.
    Checking only the retained names would be enough to stop a *stripped*
    assertion from silently removing nothing, but it would leave the more
    dangerous case open: a ``Holds`` or a ``Reachable`` naming an assertion the
    model no longer carries is never looked up, so the checker passes the
    remaining specification and the judgement reports the absent property as
    holding.

    Parameters
    ----------
    lines : list[str]
        The specification's lines, endings included, after the constant and
        fairness rewrites.
    configuration : Configuration
        The variant whose expectations are looked up.

    Raises
    ------
    MaterializeError
        If any ``Holds``, ``FailsAs`` or ``Reachable`` names an assertion the
        source does not declare.
    """
    absent = sorted(_expected_names(configuration) - declared_assertions(lines))
    if absent:
        msg = (
            f"configuration {configuration.name!r} expects {absent[0]!r}, but "
            f"the model declares no such assertion"
        )
        raise MaterializeError(msg)


def declared_assertions(lines: list[str]) -> frozenset[str]:
    """Return every assertion name the specification declares.

    Parameters
    ----------
    lines : list[str]
        The specification's lines.

    Returns
    -------
    frozenset[str]
        The names of all ``assertion`` headers, whatever their temporal
        prefix and whether they are properties or ``exists`` probes.

    Examples
    --------
    >>> sorted(declared_assertions(["always assertion S1:", "exists assertion P:"]))
    ['P', 'S1']
    """
    return frozenset(
        match.group("name")
        for line in lines
        if (match := _ASSERTION.match(line)) is not None
    )


def _expected_names(configuration: Configuration) -> frozenset[str]:
    """Return every assertion or probe name a configuration is about.

    Parameters
    ----------
    configuration : Configuration
        The configuration to read.

    Returns
    -------
    frozenset[str]
        The ``Holds`` and ``FailsAs`` property names together with the
        ``Reachable`` probe names.
    """
    return frozenset(
        expectation.property_name
        if isinstance(expectation, (Holds, FailsAs))
        else expectation.probe_name
        for expectation in configuration.expectations
    )


def strip_assertions(lines: list[str], retained: frozenset[str]) -> list[str]:
    """Drop every assertion the run does not retain.

    Parameters
    ----------
    lines : list[str]
        The specification's lines, endings included.
    retained : frozenset[str]
        The assertions to keep.

    Returns
    -------
    list[str]
        The lines with each unstripped assertion removed whole, together with
        the blank line that separated it from the text around it, so the
        retained text keeps its spacing.

    Raises
    ------
    MaterializeError
        If a retained name matches no assertion in the source. Stripping the
        rest would then leave a specification that asserts nothing while its
        report named expectations, and the checker would report a pass.
    """
    absent = sorted(retained - declared_assertions(lines))
    if absent:
        msg = f"the model declares no assertion {absent[0]!r} to retain"
        raise MaterializeError(msg)
    kept: list[str] = []
    index = 0
    while index < len(lines):
        match = _ASSERTION.match(lines[index])
        if match is None or match.group("name") in retained:
            kept.append(lines[index])
            index += 1
            continue
        index = _skip_assertion(lines, index)
        if _leaves_a_trailing_blank(lines, index, kept):
            kept.pop()
    return kept


def _leaves_a_trailing_blank(lines: list[str], index: int, kept: list[str]) -> bool:
    """Report whether stripping has left an unmatched blank line at the end.

    An assertion that ran to the end of the text takes the blank line before
    it with it; otherwise the retained text would end in a newline the reader
    never wrote.

    Parameters
    ----------
    lines : list[str]
        The specification's lines.
    index : int
        The index of the first line after the stripped assertion.
    kept : list[str]
        The lines retained so far.

    Returns
    -------
    bool
        Whether the stripped assertion was the last content and a blank line
        now dangles.
    """
    return index == len(lines) and bool(kept) and not kept[-1].strip()


def _skip_assertion(lines: list[str], start: int) -> int:
    """Return the index just past one assertion's header and body.

    Parameters
    ----------
    lines : list[str]
        The specification's lines.
    start : int
        The index of the assertion's header.

    Returns
    -------
    int
        The index of the first line after the assertion, which is either the
        next unindented line or the end of the text.
    """
    index = start + 1
    while index < len(lines) and (
        not lines[index].strip() or lines[index][:1] in " \t"
    ):
        index += 1
    return index
