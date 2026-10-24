"""Produce one runnable specification from one source and one configuration.

FizzBee has no imports and no parameters, so every variant of the model is a
whole file. Hand-copying them would let them drift from the fixed model, so the
fixed model is the only source and each variant is a pure rewrite of it. The
rewrite touches exactly four things:

- top-level constant lines whose names the configuration gives;
- the fairness keyword of actions tagged ``# fairness: <id>``;
- the complete YAML frontmatter, so a variant never inherits a bound by
  accident;
- the assertions that are expected to fail in this run but are not the one
  failure this run exists to find.

Everything else, comments and spacing included, comes back byte for byte, so a
configuration that changes nothing is the identity. D3 is the reason for the
last bullet: FizzBee reports only the first failing property, so a known-bad
configuration keeps every assertion expected to hold and exactly one expected
failure.

A defect is refused rather than ignored. A constant the model does not declare,
a fairness tag no action carries, a level that is not ``weak``, ``strong`` or
``unfair``, and an expected failure the configuration does not declare all
raise :class:`MaterializeError`, because each one means the configuration and
the model have drifted apart and the run would check something other than what
it claims.

The frontmatter is rendered by :mod:`modelcheck.frontmatter` rather than
dumped by a YAML library, because a pure module opens no file and imports no
package it does not need.
"""

import re
import typing as typ

from modelcheck.domain import (
    FailsAs,
    Holds,
    MaterializedSpec,
    MaterializeError,
    Reachable,
)
from modelcheck.frontmatter import replace_frontmatter

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from modelcheck.domain import Configuration

_LEVELS = {
    "weak": "fair",
    "strong": "fair<strong>",
    "unfair": "",
}
"""The keyword each fairness level spells; ``unfair`` spells none."""

_FAIRNESS = re.compile(r"#\s*fairness:\s*(?P<tag>\S+)")
"""An action's fairness annotation, on the line that declares the action."""

_ACTION = re.compile(
    r"^(?P<indent>[ \t]*)(?P<modifiers>(?:[\w-]+(?:<[^>]*>)?[ \t]+)*)"
    r"action\b(?P<tail>.*)$"
)
"""An action's declaration, split into its modifiers and its rest."""

_CONSTANT = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*"
    r"(?P<value>.*?)(?P<rest>[ \t]*(?:#.*)?)$"
)
"""A top-level constant assignment, split into its name, value and comment."""

_ASSERTION = re.compile(
    r"^(?:(?:always[ \t]+eventually|always|exists)[ \t]+)?"
    r"assertion[ \t]+(?P<name>[A-Za-z_][A-Za-z0-9_]*):"
)
"""An assertion's header, in any of the three temporal prefixes."""


def materialize(
    source: str,
    configuration: Configuration,
    *,
    expected_failure: str | None,
) -> MaterializedSpec:
    r"""Rewrite one source into the specification one configuration runs.

    Parameters
    ----------
    source : str
        The fixed model, exactly as it is checked in.
    configuration : Configuration
        The constants, fairness levels, frontmatter and expectations of this
        variant.
    expected_failure : str | None
        The one assertion this run is expected to violate, or ``None`` when
        the configuration expects no violation at all.

    Returns
    -------
    MaterializedSpec
        The text to compile and the fairness tags it carries.

    Raises
    ------
    MaterializeError
        If the configuration names a constant, fairness tag or expected
        failure the source does not contain, names a failure that is not a
        ``FailsAs`` expectation of the configuration, supplies a level that is
        not ``weak``, ``strong`` or ``unfair``, or supplies no expected
        failure when the configuration expects one.

    Examples
    --------
    >>> from modelcheck import domain
    >>> configuration = domain.Configuration("head", {}, {}, {}, 1, ())
    >>> source = "action Init:\n  n = 0\n"
    >>> materialize(source, configuration, expected_failure=None).text == source
    True
    """
    declared = {
        expectation.property_name
        for expectation in configuration.expectations
        if isinstance(expectation, FailsAs)
    }
    if expected_failure is None and declared:
        msg = (
            f"configuration {configuration.name!r} expects the expected "
            f"failure {min(declared)!r}, but the run was given none"
        )
        raise MaterializeError(msg)
    if expected_failure is not None and expected_failure not in declared:
        msg = (
            f"{expected_failure!r} is not an expected failure of configuration "
            f"{configuration.name!r}"
        )
        raise MaterializeError(msg)
    lines = _rewrite_constants(
        source.splitlines(keepends=True), configuration.constants
    )
    lines, in_force = _rewrite_fairness(lines, configuration.fairness)
    if expected_failure is not None:
        retained = _retained(configuration, expected_failure)
        lines = _strip_assertions(lines, retained)
    text = replace_frontmatter("".join(lines), configuration.options)
    return MaterializedSpec(configuration.name, text, tuple(sorted(in_force)))


def _retained(configuration: Configuration, expected_failure: str) -> frozenset[str]:
    """Return the assertions one run keeps.

    D3 gives each run exactly one expected failure. Keeping every assertion
    expected to hold, every probe whose reachability is judged, and that one
    failure is what makes a known-bad run prove the defect it exists to find
    rather than whichever property the checker happened to reach first.

    Parameters
    ----------
    configuration : Configuration
        The configuration the run is materialized for.
    expected_failure : str
        The assertion the run is expected to violate.

    Returns
    -------
    frozenset[str]
        The properties expected to hold, the probes and the expected failure.
    """
    held = {
        expectation.property_name
        for expectation in configuration.expectations
        if isinstance(expectation, Holds)
    }
    probed = {
        expectation.probe_name
        for expectation in configuration.expectations
        if isinstance(expectation, Reachable)
    }
    return frozenset(held | probed | {expected_failure})


def _rewrite_constants(
    lines: list[str], constants: cabc.Mapping[str, str]
) -> list[str]:
    """Substitute the configured top-level constants.

    Parameters
    ----------
    lines : list[str]
        The specification's lines, endings included.
    constants : collections.abc.Mapping[str, str]
        The name and replacement source text of each constant to substitute.

    Returns
    -------
    list[str]
        The lines with the named constants replaced and everything else
        untouched, inline comments included.

    Raises
    ------
    MaterializeError
        If a name matches no top-level assignment in the source, since the
        variant would otherwise run with the fixed model's value while its
        report claimed otherwise.
    """
    found = {
        match.group("name")
        for line in lines
        if (match := _CONSTANT.match(line)) is not None
    }
    missing = sorted(set(constants) - found)
    if missing:
        msg = f"the model declares no constant {missing[0]!r}"
        raise MaterializeError(msg)
    rewritten: list[str] = []
    for line in lines:
        match = _CONSTANT.match(line)
        if match is not None and match.group("name") in constants:
            value = constants[match.group("name")]
            line = f"{line[: match.start('value')]}{value}{line[match.end('value') :]}"
        rewritten.append(line)
    return rewritten


def _rewrite_fairness(
    lines: list[str], fairness: cabc.Mapping[str, str]
) -> tuple[list[str], tuple[str, ...]]:
    """Rewrite the fairness keyword of every action the configuration names.

    Parameters
    ----------
    lines : list[str]
        The specification's lines, endings included.
    fairness : collections.abc.Mapping[str, str]
        Fairness tag to level.

    Returns
    -------
    tuple[list[str], tuple[str, ...]]
        The rewritten lines, and every tag whose keyword was left on.

    Raises
    ------
    MaterializeError
        If a level is not one FizzBee accepts, or a tag appears on no action.
        An unknown level would reach the checker as a syntax error in a file
        this tool wrote, and an unknown tag would leave the report naming an
        assumption the model cannot carry.
    """
    annotations = map(_FAIRNESS.search, lines)
    carried = {match.group("tag") for match in annotations if match is not None}
    unknown = sorted(set(fairness) - carried)
    if unknown:
        msg = f"no action carries the fairness tag {unknown[0]!r}"
        raise MaterializeError(msg)
    for tag, level in fairness.items():
        if level not in _LEVELS:
            msg = (
                f"fairness level {level!r} for tag {tag!r} is not one of "
                f"{sorted(_LEVELS)}"
            )
            raise MaterializeError(msg)
    in_force = tuple(tag for tag, level in fairness.items() if _LEVELS[level])
    return [_fairness_line(line, fairness) for line in lines], in_force


def _fairness_line(line: str, fairness: cabc.Mapping[str, str]) -> str:
    """Rewrite one action line's fairness keyword when its tag is named.

    The keyword is re-emitted after the other modifiers because ``atomic fair
    action`` is the only order FizzBee accepts, so a correct source is neither
    reordered nor reformatted.

    Parameters
    ----------
    line : str
        One line of the specification, ending included.
    fairness : collections.abc.Mapping[str, str]
        Fairness tag to level.

    Returns
    -------
    str
        The line with its keyword spelled for the configured level, or
        unchanged when the line carries no named tag. The annotation itself
        always survives: it is the model's own record of which assumption the
        action carries, and the report quotes it back.
    """
    annotation = _FAIRNESS.search(line)
    if annotation is None or annotation.group("tag") not in fairness:
        return line
    match = _ACTION.match(line[: annotation.start()].rstrip("\r\n"))
    if match is None:
        return line
    keyword = _LEVELS[fairness[annotation.group("tag")]]
    words = [
        word for word in match.group("modifiers").split() if not word.startswith("fair")
    ]
    if keyword:
        words.append(keyword)
    head = f"{match.group('indent')}{' '.join([*words, 'action'])}"
    return f"{head}{match.group('tail')}{line[annotation.start() :]}"


def _strip_assertions(lines: list[str], retained: frozenset[str]) -> list[str]:
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
    present = {
        match.group("name")
        for line in lines
        if (match := _ASSERTION.match(line)) is not None
    }
    absent = sorted(retained - present)
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
