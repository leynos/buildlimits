"""Translate one checker run's text and error graph into the domain.

The checker reports a verdict by printing one of a small set of lines, and the
translation is fail-closed: a run is a :class:`~modelcheck.domain.Passed` only
when it printed a completion marker and nothing that outranks it. Every other
shape is refused, because a pass is the only result that can meet a positive
expectation, and a misread failure would turn a broken model into a green
report.

The precedence is fixed, and it is what ``parse_output`` checks in order: an
``Error:`` line or a non-zero exit status proves nothing, because the run did
not compile or the tool itself failed; a safety, liveness or deadlock marker is
a counterexample, since a definite violation outranks an interruption;
``Model checker stopped`` is a run a signal cut short; a pass line or the
never-reached line completes the exploration; and output carrying none of these
is an error, never a pass.

The never-reached line is a *completion* marker even though it begins with
``FAILED``: the checker prints it after finishing the exploration, names each
``exists`` assertion it did not witness, and returns without the pass line. The
domain models it as a pass carrying ``unsatisfied_probes``, which is the only
shape from which a reachability expectation can be judged unmet.

This module is pure. It classifies text already in hand and never starts the
checker or touches the filesystem; ``fizzbee_process`` supplies the text. The
error graph is a separate document, and decoding it is
:mod:`modelcheck.adapters.fizzbee_graph`'s job rather than this module's.
"""

import re
import typing as typ

from modelcheck.adapters import fizzbee_graph
from modelcheck.adapters.fizzbee_evidence import with_evidence
from modelcheck.adapters.fizzbee_measurements import elapsed, states
from modelcheck.domain import (
    Counterexample,
    CounterexampleKind,
    Errored,
    Passed,
    Stopped,
)
from modelcheck.trace import Ending, Trace

__all__ = ["parse_output"]
"""Only :func:`parse_output` is public. A caller that needs the decoded error
graph imports :mod:`modelcheck.adapters.fizzbee_graph`, which owns that
document's format; this module takes the graph as text and returns domain
values, so its callers never handle a checker key."""

if typ.TYPE_CHECKING:
    from modelcheck.domain import CheckResult

ERROR = "Error:"
"""The prefix of every diagnostic that means the run produced no state space."""

PASSED = "PASSED: Model checker completed successfully"
"""The line a run prints when its exploration completed without a violation."""

NEVER_REACHED = "FAILED: Expected states never reached"
"""The completion marker of a run whose ``exists`` probes were not all met."""

SAFETY_FAILED = "FAILED: Model checker failed. Invariant:"
"""A safety violation, naming the assertion that was violated."""

LIVENESS_FAILED = "FAILED: Liveness check failed"
"""A liveness violation, followed by a line naming the assertion."""

DEADLOCK = "DEADLOCK detected"
"""A run with no enabled action. ``(early)`` may follow on stderr paths."""

STOPPED = "Model checker stopped"
"""A run a signal interrupted, printed by the checker's signal handler."""

_INVARIANT = re.compile(r"^Invariant(?:\s+\d+)?:\s*(?P<name>.+)$")
"""An assertion name, either ``Invariant: X`` or ``Invariant 0: X``."""

_EMPTY_TRACE = Trace({}, (), Ending.FINITE, None)
"""The path a failing run without an error graph is reported with."""


def parse_output(stdout: str, exit_status: int, error_graph: str | None) -> CheckResult:
    r"""Classify one checker run, failing closed on anything unrecognized.

    Parameters
    ----------
    stdout : str
        The checker's combined output, exactly as it printed it.
    exit_status : int
        The process exit status.
    error_graph : str | None
        The text of ``error-graph.json`` when the run wrote one, which is how a
        counterexample's path is read; ``None`` when there is none.

    Returns
    -------
    CheckResult
        The classified result.

    Examples
    --------
    A completed exploration is a pass carrying the states it explored.

    >>> parse_output("Valid Nodes: 4 Unique states: 4\n" + PASSED + "\n", 0, None)
    Passed(states=4, elapsed_seconds=0.0, unsatisfied_probes=())

    A run that could not compile is never a pass, whatever else it printed.

    >>> parse_output(ERROR + " Compilation failed\n" + PASSED + "\n", 0, None)
    Errored(detail='Error: Compilation failed')

    Output with no verdict line at all is an error rather than a silent pass.

    >>> result = parse_output("Model checking example.json\n", 0, None)
    >>> isinstance(result, Errored), result.detail.splitlines()[0]
    (True, 'no verdict line in:')
    """
    lines = stdout.splitlines()
    reason = _error(lines, exit_status)
    if reason is not None:
        return Errored(reason)
    failure = _failure(lines)
    if failure is not None:
        kind, name = failure
        return _counterexample(name, kind, error_graph, stdout)
    if _mentions(lines, STOPPED):
        return Stopped(next(line for line in lines if line.startswith(STOPPED)))
    if _mentions(lines, NEVER_REACHED):
        return Passed(states(stdout), elapsed(stdout), _probes(lines))
    if _mentions(lines, PASSED):
        return Passed(states(stdout), elapsed(stdout), ())
    return Errored(f"no verdict line in:\n{stdout}")


def _error(lines: list[str], exit_status: int) -> str | None:
    """Return why a run proved nothing, or ``None`` when it ran.

    Parameters
    ----------
    lines : list[str]
        The checker's output lines.
    exit_status : int
        The process exit status.

    Returns
    -------
    str | None
        The first ``Error:`` line, which names the cause; otherwise a note that
        the checker exited non-zero; otherwise ``None``.
    """
    for line in lines:
        if line.startswith(ERROR):
            return line
    if exit_status != 0:
        return f"the checker exited with status {exit_status}"
    return None


def _failure(lines: list[str]) -> tuple[CounterexampleKind, str] | None:
    """Return the violation a run reported, and the assertion it names.

    Parameters
    ----------
    lines : list[str]
        The checker's output lines.

    Returns
    -------
    tuple[CounterexampleKind, str] | None
        The kind the marker alone establishes and the assertion's name, or
        ``None`` when no violation was reported. A deadlock names no assertion,
        because no property was violated to produce it, so its name is empty
        and never reaches a report: a report names the expectation's own
        subject instead.

    Notes
    -----
    The kind returned for a liveness failure is provisional. Its real shape —
    a lasso or a stutter — is read from the error graph, which
    :func:`parse_output` prefers whenever one is present, and which
    :func:`_reported_kind` replaces with ``UNKNOWN`` when none was written.
    """
    for index, line in enumerate(lines):
        if line.startswith(DEADLOCK):
            return CounterexampleKind.DEADLOCK, ""
        if line.startswith(SAFETY_FAILED):
            return CounterexampleKind.FINITE, _name(line)
        if line.startswith(LIVENESS_FAILED):
            return CounterexampleKind.LASSO, _following(lines, index)
    return None


def _name(line: str) -> str:
    """Return the assertion name carried on one line.

    Parameters
    ----------
    line : str
        A line such as ``FAILED: Model checker failed. Invariant:  NeverFull``.

    Returns
    -------
    str
        The name with the marker and surrounding space removed.

    Examples
    --------
    >>> _name(SAFETY_FAILED + "  NeverFull")
    'NeverFull'
    """
    _, _, tail = line.partition("Invariant:")
    return tail.strip()


def _following(lines: list[str], index: int) -> str:
    """Return the assertion name printed after a liveness failure.

    Parameters
    ----------
    lines : list[str]
        The checker's output lines.
    index : int
        The index of the ``FAILED: Liveness check failed`` line.

    Returns
    -------
    str
        The name on the next ``Invariant:`` line, or the empty string when the
        run printed none, so a report still shows the finding without a name it
        never had.
    """
    for line in lines[index + 1 :]:
        match = _INVARIANT.match(line.strip())
        if match:
            return match.group("name").strip()
    return ""


def _probes(lines: list[str]) -> tuple[str, ...]:
    """Return the ``exists`` assertions a completed run never witnessed.

    Parameters
    ----------
    lines : list[str]
        The checker's output lines.

    Returns
    -------
    tuple[str, ...]
        Each name printed on an ``Invariant N: <name>`` line after the
        never-reached marker, in the checker's own order.
    """
    start = next(
        index for index, line in enumerate(lines) if line.startswith(NEVER_REACHED)
    )
    return tuple(
        match.group("name").strip()
        for line in lines[start + 1 :]
        if (match := _INVARIANT.match(line.strip()))
    )


def _counterexample(
    name: str, marker_kind: CounterexampleKind, error_graph: str | None, stdout: str
) -> CheckResult:
    """Return the violation a failing run supports, or why it supports none.

    Parameters
    ----------
    name : str
        The assertion the run named, empty for a deadlock.
    marker_kind : CounterexampleKind
        The kind the verdict line alone establishes.
    error_graph : str | None
        The text of ``error-graph.json``, or ``None`` when none was written.
    stdout : str
        The checker's output, which carries the state count and elapsed time.

    Returns
    -------
    CheckResult
        The :class:`~modelcheck.domain.Counterexample` the run's evidence
        supports, or an :class:`~modelcheck.domain.Errored` when its graph does
        not parse. Absence and malformation differ: a run that wrote no graph
        keeps its violation and loses only the shape the graph would have
        settled, while a graph that exists but does not parse proves nothing
        rather than reporting a guessed shape.

    Examples
    --------
    >>> result = _counterexample("L3", CounterexampleKind.LASSO, None, "")
    >>> result.kind is CounterexampleKind.UNKNOWN
    True
    """
    if error_graph is None:
        return Counterexample(
            name,
            _reported_kind(marker_kind, None),
            _EMPTY_TRACE,
            states(stdout),
            elapsed(stdout),
        )
    # Untrusted JSON: a scalar, a non-mapping link or a missing key raises
    # before any shape check can name it, and each is malformed evidence.
    try:
        path = fizzbee_graph.trace(error_graph)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        # The graph is deleted with the scratch directory after this call, so
        # both the checker's own words and the malformed document are the last
        # evidence this run can be diagnosed from.
        return Errored(
            with_evidence(
                f"the error graph could not be parsed: {error}",
                (
                    ("the checker printed", stdout),
                    ("the error graph held", error_graph),
                ),
            )
        )
    return Counterexample(
        name, _reported_kind(marker_kind, path), path, states(stdout), elapsed(stdout)
    )


def _reported_kind(
    marker_kind: CounterexampleKind, trace: Trace | None
) -> CounterexampleKind:
    """Return the counterexample kind a failing run's evidence establishes.

    Parameters
    ----------
    marker_kind : CounterexampleKind
        The kind the verdict line alone establishes.
    trace : Trace | None
        The path parsed from the run's error graph, or ``None`` when the run
        wrote none.

    Returns
    -------
    CounterexampleKind
        A deadlock keeps its marker's kind even with a graph, which ends
        finitely only as a consequence of no action being enabled. Otherwise
        the graph's ending settles a shape the line could not; with no graph a
        liveness line is ``UNKNOWN`` and safety names its own shape.

    Examples
    --------
    >>> _reported_kind(CounterexampleKind.LASSO, None)
    <CounterexampleKind.UNKNOWN: 'unknown'>
    >>> _reported_kind(CounterexampleKind.DEADLOCK, _EMPTY_TRACE)
    <CounterexampleKind.DEADLOCK: 'deadlock'>
    """
    if marker_kind is CounterexampleKind.DEADLOCK:
        return CounterexampleKind.DEADLOCK
    if trace is None:
        return (
            CounterexampleKind.UNKNOWN
            if marker_kind is CounterexampleKind.LASSO
            else marker_kind
        )
    return _kind(trace)


def _kind(trace: Trace) -> CounterexampleKind:
    """Return the counterexample kind a path's ending establishes.

    Parameters
    ----------
    trace : Trace
        The normalized path.

    Returns
    -------
    CounterexampleKind
        The kind of the trace's ending, whose members carry the same values as
        the ending's, so the two enumerations map onto each other by value.
    """
    return CounterexampleKind(trace.ending.value)


def _mentions(lines: list[str], marker: str) -> bool:
    """Report whether any line starts with a marker.

    Parameters
    ----------
    lines : list[str]
        The checker's output lines.
    marker : str
        The marker to look for.

    Returns
    -------
    bool
        Whether the output carries it.
    """
    return any(line.startswith(marker) for line in lines)
