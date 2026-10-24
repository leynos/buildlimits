"""Render the matrices a human reads.

Two reports matter. The check report is one line per expectation — the
configuration, the property, the verdict, then either the state count of a
pass or the kind and length of a counterexample, and finally the fairness
assumptions the run carried — followed by a summary. The drift report names
the production surface the model no longer accounts for, the mappings that no
longer resolve, and the exclusions with their reasons.

Both renderers take values and return text. They are pure so that the exact
wording is pinned by a snapshot rather than by a subprocess's stream, and so
that a report can be rendered in a test without a checker.

Every line names its fairness, including ``none``: a reader has to be able to
tell a run that made no assumption from a report that forgot to say, and an
absent column reads as the second.
"""

import typing as typ

from modelcheck.domain import Counterexample, Passed, Reachable

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from modelcheck.domain import Expectation, Judgement
    from modelcheck.drift import DriftReport

_CONFIGURATION_WIDTH = 32
"""The column the configuration name occupies before the property."""

_PROPERTY_WIDTH = 3
"""The column a property or probe name occupies before the verdict."""

_VERDICT_WIDTH = 27
"""The column the verdict occupies before its evidence."""

_COLUMN_GAP = "  "
"""The space between two report columns."""

_NO_FAIRNESS = "none"
"""What the fairness column says when the run carried no assumption."""


def render_report(judgements: cabc.Sequence[Judgement]) -> str:
    """Render one line per judgement, then the summary line.

    Parameters
    ----------
    judgements : collections.abc.Sequence[Judgement]
        The judgements to render, in configuration then property order.

    Returns
    -------
    str
        The report, newline-terminated.

    Examples
    --------
    >>> from modelcheck import domain
    >>> judgement = domain.Judgement(
    ...     "head", domain.Holds("S1"), domain.Passed(800, 0.5, ()),
    ...     domain.Verdict.HOLDS, (),
    ... )
    >>> line, summary = render_report([judgement]).splitlines()
    >>> line[:44], line[44:].lstrip()
    ('head                              S1   holds', 'states 800  fairness: none')
    >>> summary
    'model check: 1 expectation met, 0 unmet'
    """
    met = sum(1 for judgement in judgements if judgement.is_met)
    unmet = len(judgements) - met
    lines = [_line(judgement) for judgement in judgements]
    lines.append(f"model check: {_count(met, 'expectation')} met, {unmet} unmet")
    return "\n".join(lines) + "\n"


def _line(judgement: Judgement) -> str:
    """Render one judgement as one report line.

    Parameters
    ----------
    judgement : Judgement
        The judgement to render.

    Returns
    -------
    str
        The configuration, property, verdict, evidence and fairness columns.
    """
    columns = [
        f"{judgement.configuration:<{_CONFIGURATION_WIDTH}}",
        f"{_subject(judgement.expectation):<{_PROPERTY_WIDTH}}",
        f"{judgement.verdict!s:<{_VERDICT_WIDTH}}",
        *_evidence(judgement),
        _fairness(judgement),
    ]
    return _COLUMN_GAP.join(columns)


def _fairness(judgement: Judgement) -> str:
    """Return the fairness column one judgement contributes.

    Parameters
    ----------
    judgement : Judgement
        The judgement to read.

    Returns
    -------
    str
        The fairness tags the run carried, or ``none`` when it carried none.
    """
    tags = ", ".join(judgement.fairness_in_force) or _NO_FAIRNESS
    return f"fairness: {tags}"


def _subject(expectation: Expectation) -> str:
    """Return the property or probe name one expectation is about.

    Parameters
    ----------
    expectation : Expectation
        The expectation to read.

    Returns
    -------
    str
        The probe name for a ``Reachable`` expectation and the property name
        for the other two.
    """
    match expectation:
        case Reachable(probe_name=probe_name):
            return probe_name
        case _:
            return expectation.property_name


def _evidence(judgement: Judgement) -> list[str]:
    """Return the evidence columns one judgement's result contributes.

    Parameters
    ----------
    judgement : Judgement
        The judgement to read.

    Returns
    -------
    list[str]
        The state count of a pass, the kind and length of a counterexample's
        path, or nothing for a run that produced neither.
    """
    match judgement.result:
        case Passed(states=states):
            return [f"states {states}"]
        case Counterexample(kind=kind, trace=trace):
            return [f"{kind!s}, {_count(len(trace.steps), 'step')}"]
        case _:
            return []


def _count(count: int, noun: str) -> str:
    """Return a count with its noun, pluralized when the count is not one.

    Parameters
    ----------
    count : int
        The number to spell.
    noun : str
        The singular noun the number counts.

    Returns
    -------
    str
        The number and the noun, such as ``1 step`` or ``9 steps``.

    Examples
    --------
    >>> _count(1, "step"), _count(0, "step"), _count(9, "expectation")
    ('1 step', '0 steps', '9 expectations')
    """
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def render_drift(report: DriftReport) -> str:
    """Render a drift report, or say that the model and the code correspond.

    An exclusion is a considered gap rather than a finding, so a report whose
    only content is exclusions still says the model and the code correspond;
    the exclusions follow as context, so a reader can tell a considered gap
    from a forgotten one.

    Parameters
    ----------
    report : DriftReport
        The comparison to render.

    Returns
    -------
    str
        The report, newline-terminated.

    Examples
    --------
    >>> from modelcheck.drift import DriftReport
    >>> print(render_drift(DriftReport((), (), ())), end="")
    model drift: the model accounts for every production path it names
    """
    findings = len(report.unmapped) + len(report.stale)
    summary = (
        "model drift: the model accounts for every production path it names"
        if not findings
        else f"model drift: {_count(findings, 'finding')}"
    )
    lines = [
        *(f"unmapped: {path}" for path in report.unmapped),
        *(f"stale: {path}" for path in report.stale),
        *(f"excluded: {path}" for path in report.excluded),
        summary,
    ]
    return "\n".join(lines) + "\n"
