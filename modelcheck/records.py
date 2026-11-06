"""Render the matrices a job reads as JSON.

The human report and the machine-readable record are the same statements in
another notation: one judgement per row, naming the configuration, the subject,
the verdict and what the run measured. Keeping the record renderers here
rather than in :mod:`modelcheck.report` keeps each module one reader's
concern, and keeps both under the repository's file-length limit; a reader
comparing the two notations still finds them beside each other.

The rule for both renderers is that a field appears only when the result holds
the fact it records. A run that reported no state count does not claim zero,
and a run that proved nothing carries its diagnostic instead of an empty
evidence column, so a downstream reader can always tell "not measured" from
"measured as nothing".

Like the report renderers these are pure: they take values and return text or
plain data, so a record can be pinned by a test without a checker.
"""

import json
import typing as typ

from modelcheck.domain import Counterexample, Errored, Passed, Stopped, states_of
from modelcheck.report import subject

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from modelcheck.domain import CheckResult, Judgement
    from modelcheck.fixpoint_failures import FixpointFailure
    from modelcheck.trace import Trace

__all__ = ["fixpoint_record", "render_records", "trace_record"]


def render_records(
    judgements: cabc.Sequence[Judgement],
    failures: cabc.Sequence[FixpointFailure] = (),
) -> str:
    """Render the matrix as the JSON array ``--json`` writes.

    Parameters
    ----------
    judgements : collections.abc.Sequence[Judgement]
        The judgements to render, in the same order a report shows them.
    failures : collections.abc.Sequence[FixpointFailure]
        Comparison runs that failed after the matrix was judged. They are
        appended to the same array, because the artefact is the one place a
        maintainer looks and a failure the file omitted would be evidence the
        tool discarded.

    Returns
    -------
    str
        A JSON array with one object per judgement, newline-terminated. Each
        object names the configuration, the subject, the verdict and whether it
        was met, and carries the fairness in force. The run's own measurements
        follow when it made them: the state count and elapsed seconds of a pass
        or a counterexample, or the full diagnostic of a run that proved
        nothing. A field the result has no value for is omitted rather than
        rendered as zero or an empty string.

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
    records = [_record(judgement) for judgement in judgements]
    records.extend(fixpoint_record(failure) for failure in failures)
    return json.dumps(records, indent=2) + "\n"


def _record(judgement: Judgement) -> dict[str, object]:
    """Return one judgement as the object a report's record carries.

    Parameters
    ----------
    judgement : Judgement
        The judgement to render.

    Returns
    -------
    dict[str, object]
        The fields :func:`render_records` writes. ``states`` is present only
        when the result carries a count, ``elapsed_seconds`` only when it
        carries a duration, and ``detail`` only when the run gave a reason for
        producing no verdict — a field appears only when the result has the
        fact it records, so no record claims a zero it never measured.
    """
    record: dict[str, object] = {
        "configuration": judgement.configuration,
        "subject": subject(judgement.expectation),
        "verdict": str(judgement.verdict),
        "met": judgement.is_met,
        "fairness": list(judgement.fairness_in_force),
    }
    _result_fields(record, judgement.result)
    return record


def fixpoint_record(failure: FixpointFailure) -> dict[str, object]:
    """Render one failed comparison run as the object the artefact carries.

    Parameters
    ----------
    failure : modelcheck.fixpoint.FixpointFailure
        The comparison run that did not pass.

    Returns
    -------
    dict[str, object]
        The configuration, the stage and bound of the run that failed, and the
        result's own measurements. ``subject`` is ``fixpoint`` so a reader can
        tell a comparison run from a matrix judgement, and ``met`` is always
        false: the record exists only because the run did not pass.

    Examples
    --------
    >>> from modelcheck import domain
    >>> from modelcheck.fixpoint_failures import FixpointFailure
    >>> failure = FixpointFailure(
    ...     "head", "doubled", 16, ("F1",), domain.Errored("no verdict line")
    ... )
    >>> record = fixpoint_record(failure)
    >>> record["subject"], record["stage"], record["bound"], record["detail"]
    ('fixpoint', 'doubled', 16, 'no verdict line')
    """
    record: dict[str, object] = {
        "configuration": failure.configuration,
        "subject": "fixpoint",
        "verdict": "failed",
        "met": False,
        "fairness": list(failure.fairness),
        "stage": failure.stage,
        "bound": failure.bound,
    }
    _result_fields(record, failure.result)
    if isinstance(failure.result, Counterexample):
        record["property"] = failure.result.property_name
        record["kind"] = str(failure.result.kind)
        record["trace"] = trace_record(failure.result.trace)
    return record


def _result_fields(record: dict[str, object], result: CheckResult) -> None:
    """Add the measurements one result contributes to a record.

    Parameters
    ----------
    record : dict[str, object]
        The record to extend, in place.
    result : modelcheck.domain.CheckResult
        The result to read.
    """
    states = states_of(result)
    if states is not None:
        record["states"] = states
    match result:
        case Passed(elapsed_seconds=elapsed) | Counterexample(elapsed_seconds=elapsed):
            record["elapsed_seconds"] = elapsed
        case Stopped(detail=detail) | Errored(detail=detail):
            record["detail"] = detail


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
