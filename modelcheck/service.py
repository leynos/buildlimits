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

Every run of one configuration must also report the same state count.
Stripping assertions must not change the state graph, so a mismatch means the
runs explored different models and none of them can be judged (D3).

Nothing here opens a file, starts a process or reads the clock. The model text,
the configurations and the checker all arrive through the ports, which is what
lets the whole matrix be exercised against a recording.
"""

import typing as typ

from modelcheck.domain import (
    CheckResult,
    Errored,
    FailsAs,
    Judgement,
    MaterializedSpec,
    MaterializeError,
    states_of,
)
from modelcheck.expectations import judge, only_failures
from modelcheck.materialize import materialize

if typ.TYPE_CHECKING:
    from modelcheck.domain import Configuration, Expectation
    from modelcheck.ports import ConfigurationSource, ModelChecker

"""One materialized specification and what the checker made of it.

The specification travels with the result because a judgement quotes the
fairness the run carried, and that is read from the specification rather than
from the result.
"""
type Run = tuple[MaterializedSpec, CheckResult]


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
        If the model declares no configurations at all, or a configuration
        cannot be applied to it — an unknown constant, an unknown fairness tag,
        an expectation the model does not declare, or no expectations at all.
        A configuration that expects nothing would contribute no row, and a
        report that omitted it would be indistinguishable from one where it
        passed. A model with no configurations is the same defect one level up:
        the run would ask the checker nothing and still exit ``0``.

    Examples
    --------
    >>> from modelcheck import domain
    >>> class Source:
    ...     def source_text(self):
    ...         return (
    ...             "action Init:\n  n = 0\n\n"
    ...             "always assertion S1:\n  return n == 0\n"
    ...         )
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
    """
    text = source.source_text()
    configurations = source.configurations()
    if not configurations:
        msg = (
            "the model declares no configurations, so the matrix would check "
            "nothing and report a pass without asking the checker"
        )
        raise MaterializeError(msg)
    return tuple(
        judgement
        for configuration in configurations
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
    wanted: list[str | None] = [] if only_failures(configuration) else [None]
    wanted.extend(
        expectation.property_name
        for expectation in configuration.expectations
        if isinstance(expectation, FailsAs)
    )
    runs = {
        expected_failure: _checked(
            text, configuration, checker, expected_failure=expected_failure
        )
        for expected_failure in wanted
    }
    return _consistent(configuration, runs)


def _consistent(
    configuration: Configuration, runs: dict[str | None, Run]
) -> dict[str | None, Run]:
    """Refuse a configuration whose runs explored different state counts (D3).

    Every run of one configuration strips a different set of assertions, and
    the state graph must not change with them: if it did, a verdict would be
    about a different model from the one the report names. Comparing the
    counts is the cheap proof that it did not, so a mismatch is reported as a
    run that proved nothing rather than as a pass or a finding.

    A run that reported no count at all is left alone. It is already an error
    or a stop, whose own detail says what went wrong, and replacing it with a
    count mismatch would hide the cause.

    Parameters
    ----------
    configuration : Configuration
        The configuration whose runs are compared.
    runs : dict[str | None, Run]
        The runs, keyed by the expected failure each was built for.

    Returns
    -------
    dict[str | None, Run]
        The runs unchanged when their counts agree or one is missing, and the
        same specifications paired with one ``Errored`` describing the
        mismatch otherwise.
    """
    counts = {
        expected_failure: states_of(result)
        for expected_failure, (_, result) in runs.items()
    }
    if None in counts.values() or len(set(counts.values())) == 1:
        return runs
    detail = (
        f"configuration {configuration.name!r} explored different state counts "
        f"across its runs ({_spelled(counts)}), so stripping assertions changed "
        f"the state graph and no run can be judged"
    )
    return {key: (spec, Errored(detail)) for key, (spec, _) in runs.items()}


def _spelled(counts: dict[str | None, int | None]) -> str:
    """Name each run's state count for a mismatch report.

    Parameters
    ----------
    counts : dict[str | None, int | None]
        The state count of each run, keyed by the expected failure it was
        built for.

    Returns
    -------
    str
        One ``<run>: <count> states`` clause per run, in the order the runs
        were made, with the positive run named rather than left blank.

    Examples
    --------
    >>> _spelled({None: 8, "L3": 6})
    'the positive run: 8 states, expecting L3: 6 states'
    """
    return ", ".join(
        f"{'the positive run' if key is None else f'expecting {key}'}: {count} states"
        for key, count in counts.items()
    )


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
