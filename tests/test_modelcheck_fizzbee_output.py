"""The checker's output is classified fail-closed (obligation T1).

A pass is the only result that can meet a positive expectation, so every other
way a run can end has to be recognised and refused. The parser is tested by
construction rather than by example: an enumeration names every marker the
pinned checker can print, and the test sweeps every subset of them against both
exit statuses, computing the expected classification from which markers it
inserted.

Three things make this enumeration honest.

First, it is derived from the checker's own source and confirmed against the
captured fixtures, so a marker the checker stops printing fails the suite.

Second, the presence combinations are all of them: a parser that
short-circuited on the first marker it found would still pass a test that only
ever inserted one.

Third, the seven classes are the checker's real ones, not the plan's original
five. ``FAILED`` is three distinct verdicts, and one of them —
``FAILED: Expected states never reached`` — is a *completion* marker that
leaves the domain a ``Passed`` result carrying unsatisfied probes. Folding it
in with the invariant failure would make ``Verdict.UNREACHABLE``
unreachable. See D22 in the ExecPlan.

Fourth, this sweep is one of two halves. It covers the shapes the checker
*could* print; ``test_modelcheck_fizzbee_fixtures`` covers the shapes it *did*
print, so a marker that changes shape fails one half or the other rather than
passing because the enumeration was edited to match.

Truncation is deliberately absent from the enumeration. The pinned checker
prints no truncation marker — its expander silently skips a node past
``max_actions`` — so ``tests/fixtures/fizzbee/truncation-safety`` is a plain
``PASSED`` run over an unexplored state space. The floor catches it, in
``judge``, and the companion module pins that division: the parser stays
faithful to the output, and the floor refuses the pass.
"""

import typing as typ

import pytest
from modelcheck_doubles import class_of

if typ.TYPE_CHECKING:
    import collections.abc as cabc

MARKERS: typ.Final = {
    "passed": "PASSED: Model checker completed successfully",
    "never_reached": "FAILED: Expected states never reached",
    "safety": "FAILED: Model checker failed. Invariant:  NeverFull",
    "liveness": "FAILED: Liveness check failed",
    "deadlock": "DEADLOCK detected",
    "stopped": "Model checker stopped",
    "error": "Error: Compilation failed",
}
"""Every verdict line the pinned checker prints, one per class.

Each entry is a line a capture really carries, not a paraphrase, so a marker
that changes shape between checker releases fails the sweep instead of being
silently tolerated.

``never_reached`` is the class the plan's five-class list did not have. It is
printed when an ``exists`` probe was never satisfied, and the checker returns
without printing the pass line — but the exploration did complete, so the
domain records it as a pass with ``unsatisfied_probes`` populated.

``stopped`` is real but rare: it is set only from the SIGINT/SIGTERM handler,
which no capture can produce without signalling a live run.
"""

MARKER_CLASSES: typ.Final = tuple(MARKERS)
"""The marker classes in a fixed order, so the sweep is generated, not chosen."""

QUIET_LINE: typ.Final = "Writen 1 node files and 1 link files to dir out/run_1"
"""A line the checker prints after a verdict, making output capture-shaped."""


def _subsets() -> cabc.Iterator[frozenset[str]]:
    """Yield every subset of :data:`MARKER_CLASSES`, once each.

    Yields
    ------
    frozenset[str]
        One subset per presence combination, ``2 ** 7`` of them.
    """
    for bits in range(1 << len(MARKER_CLASSES)):
        yield frozenset(
            name for index, name in enumerate(MARKER_CLASSES) if bits & (1 << index)
        )


def _output(markers: frozenset[str]) -> str:
    """Return capture-shaped checker output carrying the named marker lines.

    Parameters
    ----------
    markers : frozenset[str]
        Which marker classes to insert.

    Returns
    -------
    str
        A transcript in the checker's shape. The lines appear in the checker's
        own order, so a parser that reads them positionally sees what a real
        run would give it.
    """
    lines = ["Model checking example.json", "Valid Nodes: 4 Unique states: 4"]
    lines.extend(MARKERS[name] for name in MARKER_CLASSES if name in markers)
    lines.append(QUIET_LINE)
    return "\n".join(lines) + "\n"


def _expected(markers: frozenset[str], exit_status: int) -> str:
    """Return the result class the output must classify as.

    Parameters
    ----------
    markers : frozenset[str]
        Which marker classes the output carries.
    exit_status : int
        The process exit status.

    Returns
    -------
    str
        The class name, derived from the inserted markers and the status rather
        than from the parser, so the oracle cannot inherit the parser's
        mistakes.

    Notes
    -----
    The precedence is fail-closed and is asserted as such by the sweep: an
    error outranks a finding, because a run that did not compile proved
    nothing; a finding outranks a stop, because a definite violation outranks
    an interruption; a stop outranks a pass, because a cut run proves nothing;
    and output with none of these is an error, never a pass.
    """
    if exit_status != 0 or "error" in markers:
        return "Errored"
    if markers & {"safety", "liveness", "deadlock"}:
        return "Counterexample"
    if "stopped" in markers:
        return "Stopped"
    if markers & {"passed", "never_reached"}:
        return "Passed"
    return "Errored"


@pytest.mark.parametrize("exit_status", [0, 1])
@pytest.mark.parametrize(
    "markers", list(_subsets()), ids=lambda s: "+".join(sorted(s)) or "none"
)
def test_classification_fails_closed(markers: frozenset[str], exit_status: int) -> None:
    """Every presence combination classifies as its markers and status require.

    The oracle reads the inserted markers directly, so it cannot inherit the
    parser's mistakes. A parser that treated an unrecognised line as a pass, or
    that let the pass line override a verdict, would disagree with it on at
    least one of these rows.
    """
    from modelcheck.adapters import fizzbee_output

    result = fizzbee_output.parse_output(_output(markers), exit_status, None)

    assert class_of(result) == _expected(markers, exit_status), (
        f"markers {sorted(markers)} with exit {exit_status}"
    )


def test_the_sweep_covers_every_subset() -> None:
    """The generator yields every subset exactly once.

    A non-vacuity control on the sweep itself. If the generator missed a
    combination, the fail-closed claim would be untested for it, and nothing in
    the parametrized test above would say so.
    """
    subsets = set(_subsets())

    assert len(subsets) == 1 << len(MARKER_CLASSES), "every presence combination"
    assert frozenset() in subsets, "the empty subset is covered"
    assert frozenset(MARKER_CLASSES) in subsets, "the full subset is covered"


def test_a_never_reached_line_is_a_pass_with_unsatisfied_probes() -> None:
    """The probe marker completes a run; it does not fail one.

    ``main.go:867`` prints ``FAILED: Expected states never reached``, names each
    probed assertion, and returns without the pass line. The exploration did
    finish, so the domain models this as a pass carrying the probes it did not
    reach — which is the only shape from which ``Verdict.UNREACHABLE`` can be
    reached. A parser that treated every ``FAILED:`` prefix as fatal would make
    the reachability expectation unimplementable, and no captured fixture would
    catch it.
    """
    from modelcheck import domain
    from modelcheck.adapters import fizzbee_output

    result = fizzbee_output.parse_output(
        "Valid Nodes: 4 Unique states: 4\n"
        "\nFAILED: Expected states never reached\n"
        "Invariant 0: StopIssued\n",
        0,
        None,
    )

    assert isinstance(result, domain.Passed), "exploration completed"
    assert result.unsatisfied_probes == ("StopIssued",), "the probe it did not reach"
    assert result.states == 4, "the run's own count, which the floor reads"
