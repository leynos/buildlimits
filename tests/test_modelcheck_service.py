"""The service's two silent-pass paths, and the fixpoint that closes the third.

An empty matrix is the quietest false pass the tooling can produce: no
configuration means no judgement, and an empty judgement sequence exits ``0``.
The service therefore raises rather than returning nothing.

The fixpoint comparison is the documented evidence that FizzBee did not
truncate exploration at ``max_actions``. FizzBee prints no marker when it stops
at the bound, so the only observable test is that doubling the bound leaves the
state count unchanged. That decision — which configurations are compared and
what twice their bound is — belongs to the service, because it is the only
module allowed to ask the checker for a second run.

The same refusals are checked through the command line, because a refusal that
reached a summary with no rows would exit ``0`` exactly as a pass does. The
entry point maps them to ``EXIT_TOOL_FAILURE``, which says the checker was never
asked rather than that the model or the code is wrong.

The checker here is a small double that answers from the bound it is handed.
It is not a recording: these tests are about the comparison the service makes,
not about classifying output, which :mod:`tests.test_modelcheck_fizzbee_output`
already owns.
"""

import collections.abc as cabc

import pytest
from modelcheck_cli_cases import SOURCE, Inventory, app, holds
from modelcheck_doubles import FakeChecker, FixedSource

from modelcheck import cli, domain, fixpoint, service

_SOURCE = """\
action Init:
  n = 0

always assertion S1:
  return n < 10

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
