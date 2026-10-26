"""Render the matrices a maintainer or a job reads.

Two reports matter. The check report is one line per expectation — the
configuration, the property, the verdict, then either the state count of a
pass or the kind and length of a counterexample, and finally the fairness
assumptions the run carried — followed by a summary. The drift report names
the production surface the model no longer accounts for, the mappings that no
longer resolve, and the exclusions with their reasons.

Each has a machine-readable form beside it: :func:`render_records` writes the
matrix as a JSON array, which is what ``--json`` produces and what a later
milestone's tests read instead of re-running the model, and
:func:`trace_record` turns one counterexample's path into the object
``--save-traces`` stores. They live here rather than in the command-line
module because they are the same statements in another notation, and a reader
comparing the two should not have to hold two vocabularies.

Every renderer takes values and returns text or plain data. They are pure so
that the exact wording is pinned by a snapshot rather than by a subprocess's
stream, and so that a report can be rendered in a test without a checker.

Every line names its fairness, including ``none``: a reader has to be able to
tell a run that made no assumption from a report that forgot to say, and an
absent column reads as the second.
"""

import json
import typing as typ

from modelcheck.domain import Counterexample, Passed, Reachable

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from modelcheck.domain import Expectation, Judgement
    from modelcheck.drift import DriftReport
    from modelcheck.trace import Trace

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


def render_records(judgements: cabc.Sequence[Judgement]) -> str:
    """Render the matrix as the JSON array ``--json`` writes.

    Parameters
    ----------
    judgements : collections.abc.Sequence[Judgement]
        The judgements to render, in the same order a report shows them.

    Returns
    -------
    str
        A JSON array with one object per judgement, newline-terminated. Each
        object names the configuration, the subject, the verdict and whether it
        was met, and carries the fairness in force and the state count when the
        run reported one — a result with no state count omits the field rather
        than claiming zero.

    Examples
    --------
    >>> import json
    >>> from modelcheck import domain
    >>> record = domain.Judgement(
    ...     "head", domain.Holds("S1"), domain.Passed(4, 0.1, ()),
    ...     domain.Verdict.HOLDS, ("F1",),
    ... )
    >>> rendered = json.loads(render_records([record]))
    >>> rendered[0]["subject"], rendered[0]["verdict"], rendered[0]["states"]
    ('S1', 'holds', 4)
    """
    return json.dumps([_record(judgement) for judgement in judgements], indent=2) + "\n"


def _record(judgement: Judgement) -> dict[str, object]:
    """Return one judgement as the object a report's record carries.

    Parameters
    ----------
    judgement : Judgement
        The judgement to render.

    Returns
    -------
    dict[str, object]
        The fields :func:`render_records` writes, with ``states`` present only
        when the result carries a count.
    """
    record: dict[str, object] = {
        "configuration": judgement.configuration,
        "subject": subject(judgement.expectation),
        "verdict": str(judgement.verdict),
        "met": judgement.is_met,
        "fairness": list(judgement.fairness_in_force),
    }
    states = _states(judgement.result)
    if states is not None:
        record["states"] = states
    return record


def _states(result: object) -> int | None:
    """Return the state count a result carries, if any.

    Parameters
    ----------
    result : CheckResult
        The result to read.

    Returns
    -------
    int | None
        The count a pass or a counterexample reported, and ``None`` for a run
        that reported none, so a record never claims a figure it lacks.
    """
    return result.states if isinstance(result, (Passed, Counterexample)) else None


def trace_record(trace: Trace) -> dict[str, object]:
    """Render one normalized counterexample path as the object a trace file holds.

    Parameters
    ----------
    trace : Trace
        The path to render.

    Returns
    -------
    dict[str, object]
        The initial state, every step's action and state, how the path ends and
        where its cycle starts. The trace is already normalized, so the record
        needs no further redaction: it carries names and states the model
        declares, not addresses or timings.

    Examples
    --------
    >>> from modelcheck.trace import Ending, Trace
    >>> trace_record(Trace({"n": 0}, (), Ending.FINITE, None))["ending"]
    'finite'
    """
    return {
        "initial": dict(trace.initial),
        "steps": [
            {"action": step.action, "state": dict(step.state)} for step in trace.steps
        ],
        "ending": str(trace.ending),
        "cycle_start": trace.cycle_start,
    }
