"""Render the matrices a maintainer or a job reads.

Two reports matter. The check report is one line per expectation — the
configuration, the property, the verdict, then either the state count of a
pass, the kind and length of a counterexample, or the first line of the
diagnostic a run that proved nothing reported, and finally the fairness
assumptions the run carried — followed by a summary. The drift report names
the production surface the model no longer accounts for, the mappings that no
longer resolve, and the exclusions with their reasons.

Each has a machine-readable form beside it, in :mod:`modelcheck.records`:
``render_records`` writes the matrix as a JSON array, which is what ``--json``
produces and what a later milestone's tests read instead of re-running the
model, and ``trace_record`` turns one counterexample's path into the object
``--save-traces`` stores. They live outside the command-line module because
they are the same statements in another notation, and a reader comparing the
two should not have to hold two vocabularies.

Every renderer takes values and returns text or plain data. They are pure so
that the exact wording is pinned by a snapshot rather than by a subprocess's
stream, and so that a report can be rendered in a test without a checker.

Every line names its fairness, including ``none``: a reader has to be able to
tell a run that made no assumption from a report that forgot to say, and an
absent column reads as the second.
"""

import typing as typ

from modelcheck.domain import (
    Counterexample,
    CounterexampleKind,
    Errored,
    Passed,
    Reachable,
    Stopped,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from modelcheck.domain import Expectation, Judgement
    from modelcheck.drift import DriftReport
    from modelcheck.fixpoint import Fixpoint

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

_DETAIL_WIDTH = 56
"""How much of an uninterpretable run's diagnostic one line may show.

A run that proved nothing still has a reason, and a report that omits it makes
the reader open the artefact to find out why a verdict was withheld. The
reason is bounded rather than free text so one long checker dump cannot push
every fairness column off the line; the untruncated diagnostic is in the JSON
record, which is what a reader acts on when the first line is not enough.
"""


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
        f"{subject(judgement.expectation):<{_PROPERTY_WIDTH}}",
        f"{judgement.verdict!s:<{_VERDICT_WIDTH}}",
        *_evidence(judgement),
        _fairness(judgement),
    ]
    return _COLUMN_GAP.join(columns)


def _fairness(judgement: Judgement) -> str:
    """Return the fairness column one judgement contributes.

    The column names the tags in force and nothing else. A level is not
    printed: ``fairness_in_force`` already excludes a tag whose level is
    ``unfair``, and the level a run used is the configuration's declaration
    rather than a result, so quoting it here would print the same fact twice
    and put a second copy of the configuration in the report. The plan's
    earlier sample shows ``F1(weak)``; that spelling predates this reasoning
    and is superseded by D23.

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


def subject(expectation: Expectation) -> str:
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

    Examples
    --------
    >>> from modelcheck import domain
    >>> subject(domain.Holds("L3")), subject(domain.Reachable("StopIssued"))
    ('L3', 'StopIssued')
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
        path, or the first line of the diagnostic a run gave for producing
        neither.
    """
    match judgement.result:
        case Passed(states=states):
            return [f"states {states}"]
        case Counterexample(kind=kind, trace=trace):
            shape = "unknown shape" if kind is CounterexampleKind.UNKNOWN else kind
            return [f"{shape!s}, {_count(len(trace.steps), 'step')}"]
        case Stopped(detail=detail) | Errored(detail=detail):
            return [_detail(detail)]
        case _:
            return []


def _detail(detail: str) -> str:
    r"""Return one line of a run's diagnostic, bounded to its column.

    The detail a result carries is the checker's own words, which may be a
    whole output transcript in the ``Errored`` case; only the first line is
    shown here, because that is the line naming the cause. A diagnostic too
    wide for the column is cut and marked with an ellipsis rather than being
    allowed to move every column after it.

    Parameters
    ----------
    detail : str
        The diagnostic the result carries.

    Returns
    -------
    str
        The first non-empty line, at most :data:`_DETAIL_WIDTH` characters,
        or a placeholder when the diagnostic is blank.

    Examples
    --------
    >>> _detail("no verdict line in:\\nModel checking example.json")
    'no verdict line in:'
    >>> len(_detail("x" * 500)) == _DETAIL_WIDTH
    True
    """
    first = next((line.strip() for line in detail.splitlines() if line.strip()), "")
    if not first:
        return "(no detail)"
    if len(first) <= _DETAIL_WIDTH:
        return first
    return f"{first[: _DETAIL_WIDTH - 1]}…"


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


def render_fixpoint(comparisons: cabc.Sequence[Fixpoint]) -> str:
    """Render one line per configuration, then the summary line.

    The two counts are printed in the order they were measured, so the reader
    can see a configuration that grew rather than compare against a remembered
    bound. ``stable`` is the only passing word; anything else names the growth.

    Parameters
    ----------
    comparisons : collections.abc.Sequence[Fixpoint]
        The comparisons to render, in configuration order.

    Returns
    -------
    str
        The report, newline-terminated.

    Examples
    --------
    >>> from modelcheck.fixpoint import Fixpoint
    >>> line, summary = render_fixpoint([Fixpoint("head", 8, 8)]).splitlines()
    >>> line
    'fixpoint head                            8 -> 8 stable'
    >>> summary
    'fixpoint check: 1 configuration stable, 0 grew'
    """
    stable = sum(1 for found in comparisons if found.is_stable)
    lines = [
        f"fixpoint {found.configuration:<{_CONFIGURATION_WIDTH}}"
        f"{found.states} -> {found.doubled_states} "
        f"{'stable' if found.is_stable else 'grew'}"
        for found in comparisons
    ]
    lines.append(
        f"fixpoint check: {_count(stable, 'configuration')} stable, "
        f"{len(comparisons) - stable} grew"
    )
    return "\n".join(lines) + "\n"


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
