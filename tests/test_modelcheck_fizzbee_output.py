"""The checker's output is classified fail-closed (obligation T1).

A pass is the only result that can meet a positive expectation, so every other
way a run can end has to be recognized and refused. The parser is tested by
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
five: ``FAILED`` is three distinct verdicts, and one of them — ``FAILED:
Expected states never reached`` — is a *completion* marker that leaves the
domain a ``Passed`` result carrying unsatisfied probes, so folding it in with
the invariant failure would make ``Verdict.UNREACHABLE`` unreachable. See D22
in the ExecPlan.

Fourth, this sweep is one of two halves. It covers the shapes the checker
*could* print; ``test_modelcheck_fizzbee_fixtures`` covers the shapes it *did*
print, so a marker that changes shape fails one half or the other rather than
passing because the enumeration was edited to match.

Truncation is deliberately absent. The pinned checker prints no truncation
marker — its expander silently skips a node past ``max_actions`` — so
``tests/fixtures/fizzbee/truncation-safety`` is a plain ``PASSED`` run over an
unexplored state space. The floor catches it, in ``judge``, and the companion
module pins that division: the parser stays faithful to the output, and the
floor refuses the pass.
"""

import typing as typ

import pytest
from modelcheck_doubles import class_of

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from syrupy.assertion import SnapshotAssertion

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
    parser's mistakes. A parser that treated an unrecognized line as a pass, or
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


@pytest.mark.parametrize(
    ("marker", "kind"),
    [
        pytest.param(MARKERS["safety"], "finite", id="safety"),
        pytest.param(MARKERS["liveness"], "unknown", id="liveness"),
    ],
)
def test_a_missing_graph_establishes_only_the_shape_its_verdict_line_can(
    marker: str, kind: str
) -> None:
    """A run without an error graph keeps its violation and loses only the shape.

    The empty path a missing graph produces ends in ``FINITE``, so reading the
    kind from it would report every graphless liveness failure as finite. The
    marker is not enough either: a liveness verdict says a temporal property
    failed but not whether the path was a lasso or a stutter, so its shape is
    ``UNKNOWN`` rather than the ``LASSO`` the marker hints at. A safety
    verdict names its own shape, so it survives.
    """
    from modelcheck import domain
    from modelcheck.adapters import fizzbee_output

    result = fizzbee_output.parse_output(
        f"Valid Nodes: 4 Unique states: 4\n{marker}\n", 0, None
    )

    assert isinstance(result, domain.Counterexample), "the violation is preserved"
    assert result.kind.value == kind, "and classified by what the line establishes"


def test_an_unknown_shape_cannot_satisfy_a_shape_specific_expectation() -> None:
    """A graphless liveness failure is unmet evidence for ``FailsAs(..., LASSO)``.

    This is the judgement the classification exists for: the violation is
    reported, but it must not discharge an expectation that names a shape the
    run never demonstrated.
    """
    from modelcheck import domain, expectations
    from modelcheck.adapters import fizzbee_output

    result = fizzbee_output.parse_output(
        f"Valid Nodes: 4 Unique states: 4\n{MARKERS['liveness']}\nInvariant: L3\n",
        0,
        None,
    )
    expectation = domain.FailsAs("L3", domain.CounterexampleKind.LASSO)
    configuration = domain.Configuration("head", {}, {}, {}, 1, (expectation,))

    assert isinstance(result, domain.Counterexample), "the run reported a violation"
    judgement = expectations.judge(
        configuration,
        expectation,
        domain.MaterializedSpec("head", "", ()),
        result,
    )
    assert judgement.verdict is domain.Verdict.WRONG_KIND, (
        "but no lasso was demonstrated"
    )


@pytest.mark.parametrize(
    "graph",
    [
        pytest.param("{not json", id="not-json"),
        pytest.param("[]", id="empty"),
        pytest.param("42", id="not-a-list"),
        pytest.param("[1]", id="link-not-mapping"),
        pytest.param('{"Type": ""}', id="not-a-link-list"),
        pytest.param("[{}]", id="no-node"),
        pytest.param('[{"Type": "", "Node": 3}]', id="node-not-mapping"),
    ],
)
def test_a_graph_that_cannot_be_parsed_is_a_run_that_proved_nothing(
    graph: str,
) -> None:
    """Malformed evidence is an error, not a counterexample of a guessed shape.

    A graph that exists but does not parse is a broken artefact: nothing in it
    can support a trace, and inventing one would let an expectation be met that
    the run never demonstrated. Every malformed shape the checker could
    conceivably write must leave through the same door, and the judgement that
    consumes it must refuse to call an expected failure demonstrated.
    """
    from modelcheck import domain, expectations
    from modelcheck.adapters import fizzbee_output

    result = fizzbee_output.parse_output(
        f"Valid Nodes: 4 Unique states: 4\n{MARKERS['safety']}\n", 0, graph
    )
    expectation = domain.FailsAs("NeverFull", domain.CounterexampleKind.FINITE)
    configuration = domain.Configuration("head", {}, {}, {}, 1, (expectation,))

    assert isinstance(result, domain.Errored), "the run proved nothing"
    assert "could not be parsed" in result.detail, "and the reason names the parse"
    judgement = expectations.judge(
        configuration,
        expectation,
        domain.MaterializedSpec("head", "", ()),
        result,
    )
    assert judgement.verdict is domain.Verdict.ERROR, (
        "and broken evidence cannot discharge an expected failure"
    )


def test_a_malformed_graph_keeps_the_evidence_that_explains_it(
    snapshot: SnapshotAssertion,
) -> None:
    """The transcript and the graph survive the scratch directory's removal.

    A run whose graph cannot be parsed is the one case where the parser holds
    evidence and would otherwise discard it: the process adapter deletes the
    scratch directory as the call returns, so the diagnostic is the only record
    of what the checker printed and what it wrote. The whole diagnostic is
    pinned, because its value is which parts it keeps and in what order.
    """
    from modelcheck import domain
    from modelcheck.adapters import fizzbee_output

    result = fizzbee_output.parse_output(
        f"Valid Nodes: 4 Unique states: 4\n{MARKERS['safety']}\n", 0, "{not json"
    )

    assert isinstance(result, domain.Errored), "the run proved nothing"
    assert result.detail == snapshot, "the reason and both captures are kept"


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


def test_a_run_with_no_verdict_keeps_a_bounded_transcript() -> None:
    """A runaway transcript is cut, and the cut is stated.

    Output carrying no verdict is an error, and the transcript is the only
    evidence of what the run printed. Keeping all of it would put a runaway
    run's whole output into the artefact, so the parser keeps bounded head and
    tail regions and says how much of the whole that is.
    """
    from modelcheck import domain
    from modelcheck.adapters import fizzbee_output

    result = fizzbee_output.parse_output("x" * 5000, 0, None)

    assert isinstance(result, domain.Errored), "no verdict line is an error"
    assert "first 2000 and last 2000 of 5000 characters kept" in result.detail, (
        "the cut is stated"
    )
    assert result.detail.count("x") == 4000, "and only the two bounded ends are kept"
