"""The service's silent-pass paths, and what closes them.

An empty matrix is the quietest false pass the tooling can produce: no
configuration means no judgement, and an empty judgement sequence exits ``0``.

Each configuration's runs must also agree on their state count (D3), because
stripping assertions must not change the state graph. The fixpoint comparison
that follows a passing matrix lives in ``test_modelcheck_fixpoint_service.py``;
this module is about the matrix itself.

The same refusals are checked through the command line, because a refusal that
reached a summary with no rows would exit ``0`` exactly as a pass does.

The checkers here are small doubles that answer from the specification they are
handed, not recordings: these tests are about the comparisons the service makes.
"""

import collections.abc as cabc

import pytest
from modelcheck_cases import trace
from modelcheck_cli_cases import SOURCE, Inventory, app, holds
from modelcheck_doubles import FakeChecker, FixedSource
from modelcheck_service_cases import Bounded, bounded_configuration, source

from modelcheck import cli, domain, fixpoint, service


class _Counted:
    """A checker whose state count depends on which run it answers.

    Parameters
    ----------
    counts : dict[str | None, int]
        The count to report for the positive run and for the targeted one.
    failure : str
        The assertion the configuration expects to fail; the run retaining it
        is the targeted run.
    kind : domain.CounterexampleKind
        The kind the targeted run reports. The default, a lasso, completed its
        exploration, so its count is comparable with a pass's.
    """

    def __init__(
        self,
        counts: dict[str | None, int],
        *,
        failure: str,
        kind: domain.CounterexampleKind = domain.CounterexampleKind.LASSO,
    ) -> None:
        """Hold the counts, the assertion and the kind each run reports."""
        self._counts = counts
        self._failure = failure
        self._kind = kind

    def check(self, spec: domain.MaterializedSpec) -> domain.CheckResult:
        """Answer a pass for the positive run, a counterexample for the other.

        Parameters
        ----------
        spec : domain.MaterializedSpec
            The specification to answer.

        Returns
        -------
        domain.CheckResult
            The result the counted run reports.
        """
        if f"assertion {self._failure}:" not in spec.text:
            return domain.Passed(self._counts[None], 0.1, ())
        return domain.Counterexample(
            self._failure,
            self._kind,
            trace(),
            self._counts[self._failure],
            0.1,
        )


_MIXED = bounded_configuration(
    expectations=(
        domain.Holds("S1"),
        domain.FailsAs("S2", domain.CounterexampleKind.LASSO),
    )
)
"""A configuration that holds ``S1`` and expects ``S2`` to fail.

The expected failure is a liveness one because both runs must complete their
exploration for their counts to be comparable at all; the safety case is
:data:`_SAFETY`.
"""

_SAFETY = bounded_configuration(
    expectations=(
        domain.Holds("S1"),
        domain.FailsAs("S2", domain.CounterexampleKind.FINITE),
    )
)
"""A configuration whose expected failure is found before exploration ends."""


@pytest.mark.parametrize("run", [service.run_matrix, fixpoint.run_fixpoint])
def test_a_model_with_no_configurations_raises(
    run: cabc.Callable[..., object],
) -> None:
    """An empty matrix is a usage error, not a pass with nothing to report.

    Both entry points reach the checker only through a configuration, so a
    model with none would ask it nothing and still return an empty sequence.
    The command line exits ``0`` on an empty sequence, which is why the refusal
    has to happen here.
    """
    with pytest.raises(domain.MaterializeError, match="no configurations"):
        run(source(), Bounded())


def test_the_matrix_keeps_one_row_per_expectation() -> None:
    """An empty matrix is refused, but a real one still reports every row."""
    two = bounded_configuration(
        expectations=(domain.Holds("S1"), domain.Reachable("StopIssued"))
    )

    judgements = service.run_matrix(source(two), Bounded())

    assert [judgement.verdict for judgement in judgements] == [
        domain.Verdict.HOLDS,
        domain.Verdict.REACHED,
    ], "one judgement per expectation, in declaration order"


def test_runs_that_explore_different_state_counts_are_refused() -> None:
    """A configuration whose runs disagree explored two different models (D3).

    Stripping assertions must not change the state graph; when it does, every
    row of the configuration is an error naming both counts.
    """
    judgements = service.run_matrix(
        source(_MIXED), _Counted({None: 80, "S2": 60}, failure="S2")
    )

    assert [judgement.verdict for judgement in judgements] == [
        domain.Verdict.ERROR,
        domain.Verdict.ERROR,
    ], "neither run can be judged"
    details = {
        judgement.result.detail
        for judgement in judgements
        if isinstance(judgement.result, domain.Errored)
    }
    assert len(details) == 1, "every row names the same mismatch"
    detail = details.pop()
    assert "the positive run: 80 states" in detail, "the positive count is named"
    assert "expecting S2: 60 states" in detail, "and the targeted count is too"


def test_runs_that_explore_the_same_state_count_are_judged() -> None:
    """Agreement is the evidence that stripping assertions changed nothing."""
    judgements = service.run_matrix(
        source(_MIXED), _Counted({None: 80, "S2": 80}, failure="S2")
    )

    assert [judgement.verdict for judgement in judgements] == [
        domain.Verdict.HOLDS,
        domain.Verdict.AS_EXPECTED,
    ], "both expectations are judged against their own run"


def test_a_safety_counterexample_count_is_not_compared() -> None:
    """A run that stopped at a violation explored no fixed number of states.

    The count a safety failure prints is how far the search had got when it
    found the violation, which is smaller than the completed run's. Comparing
    the two would refuse a configuration for finding exactly the defect it
    declared it expected, so only completed explorations are counted.
    """
    judgements = service.run_matrix(
        source(_SAFETY),
        _Counted(
            {None: 80, "S2": 60},
            failure="S2",
            kind=domain.CounterexampleKind.FINITE,
        ),
    )

    assert [judgement.verdict for judgement in judgements] == [
        domain.Verdict.HOLDS,
        domain.Verdict.AS_EXPECTED,
    ], "the partial count is not evidence the state graph changed"


def test_an_empty_matrix_is_a_usage_error() -> None:
    """A model with no configurations is refused rather than reported empty.

    The service refuses it, so the command raises rather than printing a
    summary with no rows; the message names the defect, because a maintainer's
    next step is to add the configuration rather than to debug the checker.
    """
    application = app([], FakeChecker("pass"))

    with pytest.raises(domain.MaterializeError, match="no configurations"):
        application(["check", ".", "--fizz", "/nonexistent/fizz"])


def test_an_unmatched_only_selection_is_a_usage_error() -> None:
    """A ``--only`` that matches no row is a mistyped name, not a pass.

    An empty narrowed report would exit ``0``, which a gate reads as success
    while saying only that the selection named nothing the matrix produced.
    """
    application = app([holds("head")], FakeChecker("pass"))

    with pytest.raises(ValueError, match="no such expectation"):
        application([
            "check",
            ".",
            "--fizz",
            "/nonexistent/fizz",
            "--only",
            "NoSuchExpectation",
        ])


@pytest.mark.parametrize(
    ("configurations", "only"),
    [
        pytest.param([], [], id="empty-matrix"),
        pytest.param([holds("head")], ["NoSuchExpectation"], id="unmatched-only"),
    ],
)
def test_a_refused_invocation_exits_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    configurations: list[domain.Configuration],
    only: list[str],
) -> None:
    """The entry point maps both refusals to ``2``, the "checker was not asked" status.

    ``1`` means the model or the code is wrong; ``2`` means the tool could not
    run. A gate that could not tell them apart would report a mistyped flag as
    a failing model, so the mapping is asserted through the real ``main``.
    """
    from modelcheck import __main__ as entry

    fixed = FixedSource(configurations, source=SOURCE)
    monkeypatch.setattr(
        entry,
        "_adapters",
        lambda: entry._Adapters(
            checker=lambda _path: FakeChecker("pass"),
            configurations=lambda _directory, _bounds: fixed,
            inventory=Inventory,
        ),
    )

    status = entry.main([
        "check",
        ".",
        "--fizz",
        "/nonexistent/fizz",
        *(argument for selection in only for argument in ("--only", selection)),
    ])

    assert status == cli.EXIT_TOOL_FAILURE, "the checker was never asked"
    assert "modelcheck:" in capsys.readouterr().err, "and the reason is reported"
