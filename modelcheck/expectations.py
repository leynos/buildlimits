"""Judge one checker result against one expectation.

A run answers many expectations at once, and each expectation is read
differently. A property expected to hold is met by a pass and violated by any
counterexample; a defect expected to appear is met only by a counterexample to
that property of that kind; an ``exists`` probe is met only when the run
reports it satisfied. The four outcomes also carry their own verdicts: a run
the checker cut short proves nothing, and an uninterpretable run is an error
rather than a pass.

The floor is applied only to a pass. A counterexample found below the
configuration's ``min_states`` floor is still a counterexample — the state was
reached — but a pass below the floor means exploration stopped short of the
space the configuration claims, so it proves nothing.

The materialized specification is a parameter so that a judgement cannot quote
fairness the run did not carry: judging a result against a specification built
for another configuration is a defect and raises.
"""

import typing as typ

from modelcheck.domain import (
    Counterexample,
    Errored,
    FailsAs,
    Holds,
    Judgement,
    Passed,
    Reachable,
    Stopped,
    Verdict,
)

if typ.TYPE_CHECKING:
    from modelcheck.domain import (
        CheckResult,
        Configuration,
        Expectation,
        MaterializedSpec,
    )


def judge(
    configuration: Configuration,
    expectation: Expectation,
    spec: MaterializedSpec,
    result: CheckResult,
) -> Judgement:
    """Judge one result against one expectation of one configuration.

    Parameters
    ----------
    configuration : Configuration
        The configuration the run was materialized for.
    expectation : Expectation
        What that configuration expected of this run.
    spec : MaterializedSpec
        The specification the checker ran, whose fairness the judgement
        quotes.
    result : CheckResult
        What the checker reported.

    Returns
    -------
    Judgement
        The expectation, the result, the verdict that pairs them and the
        fairness the run carried.

    Raises
    ------
    ValueError
        If ``spec`` was materialized for another configuration, so a
        judgement could never have been about this run.

    Examples
    --------
    >>> from modelcheck import domain
    >>> configuration = domain.Configuration("head", {}, {}, {}, 1, ())
    >>> judgement = judge(
    ...     configuration,
    ...     domain.Holds("S1"),
    ...     domain.MaterializedSpec("head", "", ()),
    ...     domain.Passed(800, 0.5, ()),
    ... )
    >>> judgement.verdict is domain.Verdict.HOLDS
    True
    """
    if spec.configuration != configuration.name:
        msg = (
            f"the specification was materialized for {spec.configuration!r}, "
            f"not {configuration.name!r}"
        )
        raise ValueError(msg)
    return Judgement(
        configuration=configuration.name,
        expectation=expectation,
        result=result,
        verdict=_verdict(configuration, expectation, result),
        fairness_in_force=spec.fairness_in_force,
    )


def _verdict(
    configuration: Configuration,
    expectation: Expectation,
    result: CheckResult,
) -> Verdict:
    """Return the verdict one result earns from one expectation.

    Parameters
    ----------
    configuration : Configuration
        The configuration the run was materialized for.
    expectation : Expectation
        What that configuration expected of this run.
    result : CheckResult
        What the checker reported.

    Returns
    -------
    Verdict
        The verdict that pairs them. A result outside the declared union is
        reported as ``ERROR`` rather than guessed at; the truth-table test is
        what makes a newly declared variant fail loudly instead.
    """
    match result:
        case Errored():
            return Verdict.ERROR
        case Stopped():
            return Verdict.STOPPED
        case Passed():
            return _pass_verdict(configuration, expectation, result)
        case Counterexample():
            return _failure_verdict(expectation, result)
        case _:
            return Verdict.ERROR


def _pass_verdict(
    configuration: Configuration,
    expectation: Expectation,
    result: Passed,
) -> Verdict:
    """Return the verdict a pass earns from one expectation.

    Parameters
    ----------
    configuration : Configuration
        The configuration the run was materialized for.
    expectation : Expectation
        What that configuration expected of this run.
    result : Passed
        The pass the checker reported.

    Returns
    -------
    Verdict
        ``MISSING`` when a defect was expected and is gone, ``UNREACHABLE``
        when the named probe is unsatisfied, ``BELOW_FLOOR`` when exploration
        stopped short, and otherwise the verdict the expectation earns from a
        pass.
    """
    match expectation:
        case FailsAs():
            return Verdict.MISSING
        case Reachable(probe_name=probe_name):
            if probe_name in result.unsatisfied_probes:
                return Verdict.UNREACHABLE
            return _above_floor(configuration, result, Verdict.REACHED)
        case Holds():
            return _above_floor(configuration, result, Verdict.HOLDS)
        case _:
            return Verdict.ERROR


def _above_floor(
    configuration: Configuration,
    result: Passed,
    verdict: Verdict,
) -> Verdict:
    """Return a pass's verdict, or the floor's verdict when it explored too little.

    Parameters
    ----------
    configuration : Configuration
        The configuration whose floor the pass must clear.
    result : Passed
        The pass the checker reported.
    verdict : Verdict
        The verdict to return when the pass clears the floor.

    Returns
    -------
    Verdict
        ``BELOW_FLOOR`` when the run explored fewer states than the
        configuration's floor, and ``verdict`` otherwise.
    """
    if result.states < configuration.min_states:
        return Verdict.BELOW_FLOOR
    return verdict


def _failure_verdict(expectation: Expectation, result: Counterexample) -> Verdict:
    """Return the verdict a counterexample earns from one expectation.

    Parameters
    ----------
    expectation : Expectation
        What the configuration expected of this run.
    result : Counterexample
        The violation the checker reported.

    Returns
    -------
    Verdict
        ``AS_EXPECTED`` when the named property failed with the named kind,
        ``WRONG_KIND`` when it failed with another kind, and ``UNEXPECTED``
        when another property failed, which no configuration expects.
    """
    match expectation:
        case FailsAs(property_name=property_name, kind=kind):
            if result.property_name != property_name:
                return Verdict.UNEXPECTED
            return Verdict.AS_EXPECTED if result.kind is kind else Verdict.WRONG_KIND
        case _:
            return Verdict.UNEXPECTED
