"""The service's silent-pass paths, and the comparisons that close them.

An empty matrix is the quietest false pass the tooling can produce: no
configuration means no judgement, and an empty judgement sequence exits ``0``.

The fixpoint comparison is the documented evidence that FizzBee did not
truncate exploration at ``max_actions``: it prints no marker when it stops, so
the only observable test is that doubling the bound leaves the count unchanged.
The same reasoning makes each configuration's runs agree on their state count
(D3), because stripping assertions must not change the state graph.

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

from modelcheck import cli, domain, fixpoint, service

_SOURCE = """\
action Init:
  n = 0

always assertion S1:
  return n < 10

always assertion S2:
  return n < 20

exists assertion StopIssued:
  return n == 3
"""
"""A model declaring the assertion and the probe these configurations name."""


class _Bounded:
    """A checker whose state count follows the bound in the specification.

    Parameters
    ----------
    grows : bool
        Whether doubling the bound also doubles the state count. ``False``
        models a run that reached its fixpoint.
    """

    def __init__(self, *, grows: bool = False) -> None:
        """Hold the growth rule and the specifications seen."""
        self.spec_texts: list[str] = []
        self._grows = grows

    def check(self, spec: domain.MaterializedSpec) -> domain.CheckResult:
        """Answer a pass, counting states from the bound it was given.

        Parameters
        ----------
        spec : domain.MaterializedSpec
            The specification to answer.

        Returns
        -------
        domain.CheckResult
            A pass whose count is larger at the doubled bound when the double
            grows.
        """
        self.spec_texts.append(spec.text)
        doubled = "max_actions: 16" in spec.text
        if not doubled:
            return domain.Passed(80, 0.1, ())
        return domain.Passed(160 if self._grows else 80, 0.1, ())


class _Refuses:
    """A checker that reports a run which proved nothing."""

    def check(self, spec: domain.MaterializedSpec) -> domain.CheckResult:
        """Answer with an error regardless of the specification.

        Parameters
        ----------
        spec : domain.MaterializedSpec
            The specification this run would have checked.

        Returns
        -------
        domain.CheckResult
            An error, which no fixpoint comparison may read a count from.
        """
        del spec
        return domain.Errored("no verdict line")


class _Counted:
    """A checker whose state count depends on which run it answers.

    Parameters
    ----------
    counts : dict[str | None, int]
        The count to report for the positive run and for the targeted one.
    failure : str
        The assertion the configuration expects to fail; the run retaining it
        is the targeted run.
    """

    def __init__(self, counts: dict[str | None, int], *, failure: str) -> None:
        """Hold the counts and the assertion that identifies the targeted run."""
        self._counts = counts
        self._failure = failure

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
            domain.CounterexampleKind.FINITE,
            trace(),
            self._counts[self._failure],
            0.1,
        )


def _source(*configurations: domain.Configuration) -> FixedSource:
    """Return a source holding the given configurations.

    Parameters
    ----------
    *configurations : domain.Configuration
        The configurations the matrix runs.

    Returns
    -------
    FixedSource
        The source, over the module's model text.
    """
    return FixedSource(configurations, source=_SOURCE)


def _configuration(
    name: str = "head",
    *,
    max_actions: int | None = 8,
    expectations: tuple[domain.Expectation, ...] = (domain.Holds("S1"),),
) -> domain.Configuration:
    """Build one configuration with an optional action bound.

    Parameters
    ----------
    name : str
        The configuration's name.
    max_actions : int | None
        The exploration bound to declare, or ``None`` to declare none.
    expectations : tuple[domain.Expectation, ...]
        What the configuration expects of its run.

    Returns
    -------
    domain.Configuration
        The configuration.
    """
    options = {} if max_actions is None else {"options": {"max_actions": max_actions}}
    return domain.Configuration(name, {}, {}, options, 1, expectations)


_MIXED = _configuration(
    expectations=(
        domain.Holds("S1"),
        domain.FailsAs("S2", domain.CounterexampleKind.FINITE),
    )
)
"""A configuration that holds ``S1`` and expects ``S2`` to fail."""


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
        run(_source(), _Bounded())


def test_the_fixpoint_doubles_the_declared_bound() -> None:
    """Twice the declared ``max_actions`` is what the second run is given.

    The bound is read from the configuration's frontmatter rather than being a
    constant, so a configuration that raised its bound is compared against its
    own number rather than the plan's.
    """
    checker = _Bounded()

    fixpoint.run_fixpoint(_source(_configuration(max_actions=8)), checker)

    assert [text.count("max_actions: 8") for text in checker.spec_texts] == [1, 0], (
        "the declared run keeps its bound"
    )
    assert [text.count("max_actions: 16") for text in checker.spec_texts] == [0, 1], (
        "and the comparison run is given twice it"
    )


def test_a_stable_count_is_reported_as_stable() -> None:
    """An unchanged count at the doubled bound is the fixpoint evidence."""
    found = fixpoint.run_fixpoint(_source(_configuration()), _Bounded())

    assert found == (fixpoint.Fixpoint("head", 80, 80),), "both counts are reported"
    assert found[0].is_stable, "and the comparison passes"


def test_a_count_that_grows_with_the_bound_is_not_stable() -> None:
    """A run cut short at its bound explores more when the bound is raised."""
    found = fixpoint.run_fixpoint(_source(_configuration()), _Bounded(grows=True))

    assert found[0].states != found[0].doubled_states, "the growth is visible"
    assert not found[0].is_stable, "and the comparison fails"


def test_a_configuration_without_an_action_bound_cannot_be_compared() -> None:
    """Doubling a bound that is absent would compare two identical runs."""
    with pytest.raises(domain.MaterializeError, match="max_actions"):
        fixpoint.run_fixpoint(_source(_configuration(max_actions=None)), _Bounded())


def test_only_configurations_that_should_pass_are_compared() -> None:
    """A mutant's run stops at its counterexample, so it is not compared.

    Its state count says how far the checker got before the violation, which is
    not a count of the fixed model's states. Asking for a second run of it
    would also risk the doubled bound hiding the very defect it exists to find.
    """
    mutant = _configuration(
        "incident-1",
        expectations=(domain.FailsAs("S1", domain.CounterexampleKind.LASSO),),
    )
    checker = _Bounded()

    found = fixpoint.run_fixpoint(_source(mutant), checker)

    assert not found, "no comparison is made for a known-bad configuration"
    assert not checker.spec_texts, "and the checker is never asked"


def test_a_run_that_does_not_pass_cannot_be_compared() -> None:
    """A count from a run that proved nothing would compare two errors."""
    with pytest.raises(domain.MaterializeError, match="did not pass"):
        fixpoint.run_fixpoint(_source(_configuration()), _Refuses())


def test_the_matrix_keeps_one_row_per_expectation() -> None:
    """An empty matrix is refused, but a real one still reports every row."""
    two = _configuration(
        expectations=(domain.Holds("S1"), domain.Reachable("StopIssued"))
    )

    judgements = service.run_matrix(_source(two), _Bounded())

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
        _source(_MIXED), _Counted({None: 80, "S2": 60}, failure="S2")
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
        _source(_MIXED), _Counted({None: 80, "S2": 80}, failure="S2")
    )

    assert [judgement.verdict for judgement in judgements] == [
        domain.Verdict.HOLDS,
        domain.Verdict.AS_EXPECTED,
    ], "both expectations are judged against their own run"


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

    source = FixedSource(configurations, source=SOURCE)
    monkeypatch.setattr(
        entry,
        "_adapters",
        lambda: entry._Adapters(
            checker=lambda _path: FakeChecker("pass"),
            configurations=lambda _directory, _bounds: source,
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
