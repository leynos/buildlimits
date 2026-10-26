"""Run every configuration of one model and judge what each run produced.

The service is the only place that decides *how many* checker runs a
configuration costs, and that decision is D3's: a configuration that expects a
defect costs one run per defect, because FizzBee reports the first failing
property and a run carrying two of them could produce the wrong one. Every
other configuration costs a single run, whose one result answers all of its
``Holds`` and ``Reachable`` expectations at once.

A configuration that mixes the two kinds costs one run for the expectations
nothing should violate, plus one run per expected defect. Judging a ``Holds``
expectation against a run built to fail would report an ``UNEXPECTED``
counterexample for an expectation the configuration never claimed, so the two
kinds are read from different runs.

Nothing here opens a file, starts a process or reads the clock. The model text,
the configurations and the checker all arrive through the ports, which is what
lets the whole matrix be exercised against a recording.
"""

import typing as typ

from modelcheck.domain import (
    CheckResult,
    FailsAs,
    Judgement,
    MaterializedSpec,
    MaterializeError,
)
from modelcheck.expectations import judge
from modelcheck.materialize import materialize

if typ.TYPE_CHECKING:
    from modelcheck.domain import Configuration, Expectation
    from modelcheck.ports import ConfigurationSource, ModelChecker

type Run = tuple[MaterializedSpec, CheckResult]
"""One materialized specification and what the checker made of it.

The specification travels with the result because a judgement quotes the
fairness the run carried, and that is read from the specification rather than
from the result.
"""


def run_matrix(
    source: ConfigurationSource, checker: ModelChecker
) -> tuple[Judgement, ...]:
    r"""Run every configuration and judge every expectation of each.

    Parameters
    ----------
    source : ConfigurationSource
        The model text and the configurations to run.
    checker : ModelChecker
        The checker every run is delegated to.

    Returns
    -------
    tuple[Judgement, ...]
        One judgement per expectation, in configuration order and then in the
        order the configuration declared its expectations, so a report reads
        in the order a reader wrote the file.

    Raises
    ------
    MaterializeError
        If a configuration cannot be applied to the model — an unknown
        constant, an unknown fairness tag, or no expectations at all. A
        configuration that expects nothing would contribute no row, and a
        report that omitted it would be indistinguishable from one where it
        passed.

    Examples
    --------
    >>> from modelcheck import domain
    >>> class Source:
    ...     def source_text(self):
    ...         return "action Init:\\n  n = 0\\n"
    ...     def configurations(self):
    ...         return (
    ...             domain.Configuration(
    ...                 "head", {}, {}, {}, 1, (domain.Holds("S1"),)
    ...             ),
    ...         )
    ...     def exclusions(self):
    ...         return {}
    >>> class Checker:
    ...     def check(self, spec):
    ...         return domain.Passed(4, 0.5, ())
    >>> [judgement.verdict for judgement in run_matrix(Source(), Checker())]
    [<Verdict.HOLDS: 'holds'>]
    """  # ruff: ignore[docstring-extraneous-exception] - materialize raises it.
    text = source.source_text()
    return tuple(
        judgement
        for configuration in source.configurations()
        for judgement in _judged(text, configuration, checker)
    )


def _judged(
    text: str, configuration: Configuration, checker: ModelChecker
) -> tuple[Judgement, ...]:
    """Judge every expectation of one configuration against the runs it needs.

    Parameters
    ----------
    text : str
        The model source.
    configuration : Configuration
        The variant to materialize and run.
    checker : ModelChecker
        The checker every run is delegated to.

    Returns
    -------
    tuple[Judgement, ...]
        One judgement per expectation, in the configuration's own order.

    Raises
    ------
    MaterializeError
        If the configuration expects nothing at all.
    """
    if not configuration.expectations:
        msg = (
            f"configuration {configuration.name!r} expects nothing, so no run "
            f"could satisfy it and no report row would show it"
        )
        raise MaterializeError(msg)
    runs = _runs(text, configuration, checker)
    return tuple(
        judge(configuration, expectation, *runs[expected_failure_of(expectation)])
        for expectation in configuration.expectations
    )


def _runs(
    text: str, configuration: Configuration, checker: ModelChecker
) -> dict[str | None, Run]:
    """Run the variants one configuration needs, keyed by expected failure.

    Parameters
    ----------
    text : str
        The model source.
    configuration : Configuration
        The variant to materialize.
    checker : ModelChecker
        The checker every run is delegated to.

    Returns
    -------
    dict[str | None, Run]
        The run for each expected failure the configuration declares, plus the
        run for no expected failure when it has an expectation nothing should
        violate.
    """
    wanted: list[str | None] = [] if _only_failures(configuration) else [None]
    wanted.extend(
        expectation.property_name
        for expectation in configuration.expectations
        if isinstance(expectation, FailsAs)
    )
    return {
        expected_failure: _checked(
            text, configuration, checker, expected_failure=expected_failure
        )
        for expected_failure in wanted
    }


def _checked(
    text: str,
    configuration: Configuration,
    checker: ModelChecker,
    *,
    expected_failure: str | None,
) -> Run:
    """Materialize one variant, run it, and keep the pair.

    Parameters
    ----------
    text : str
        The model source.
    configuration : Configuration
        The variant to materialize.
    checker : ModelChecker
        The checker the run is delegated to.
    expected_failure : str | None
        The one assertion this run is expected to violate, or ``None``.

    Returns
    -------
    Run
        The specification that was run and its classified result.
    """
    spec = materialize(text, configuration, expected_failure=expected_failure)
    return spec, checker.check(spec)


def _only_failures(configuration: Configuration) -> bool:
    """Report whether every expectation of a configuration is a failure.

    Parameters
    ----------
    configuration : Configuration
        The configuration to read.

    Returns
    -------
    bool
        Whether it has no expectation outside a ``FailsAs``, so a run that
        should violate nothing would answer none of them.
    """
    return all(
        isinstance(expectation, FailsAs) for expectation in configuration.expectations
    )


def expected_failure_of(expectation: Expectation) -> str | None:
    """Return the run one expectation is judged against.

    Parameters
    ----------
    expectation : Expectation
        The expectation to read.

    Returns
    -------
    str | None
        The property name for a ``FailsAs``, because that expectation is only
        meaningful in the run built to violate it; ``None`` otherwise, because
        a ``Holds`` or ``Reachable`` is read from the run that should violate
        nothing.

    Examples
    --------
    >>> from modelcheck import domain
    >>> expected_failure_of(domain.Holds("S1")) is None
    True
    >>> expected_failure_of(
    ...     domain.FailsAs("L3", domain.CounterexampleKind.LASSO)
    ... )
    'L3'
    """
    match expectation:
        case FailsAs(property_name=property_name):
            return property_name
        case _:
            return None
