"""The model, the source and the checker doubles the service tests share.

``test_modelcheck_service.py`` asks what the matrix does with a configuration's
runs; ``test_modelcheck_fixpoint_service.py`` asks what the comparison does
when the same configuration is run again at a doubled bound. Both need one
model text declaring the assertions they name, a source built from the
configurations under test, and a configuration that declares an exploration
bound, so those live here rather than in either test module.

The checkers are small doubles that answer from the specification they are
handed rather than recordings: these are tests about the comparisons the
service makes, not about what a real run prints. :class:`Answers` is the one
the property modules use, because it answers by assertion rather than by call
order; :func:`counterexample` and :func:`mismatch` build and read the results
those properties compare.

This module is test support and is linted as production code, following the
convention in ``docs/repository-layout.md``: ``test_*.py`` modules hold tests,
anything else here holds helpers.
"""

import typing as typ

from modelcheck_cases import configuration, trace
from modelcheck_source_doubles import FixedSource

from modelcheck import domain

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from modelcheck.domain import (
        CheckResult,
        Configuration,
        Expectation,
        MaterializedSpec,
    )

_NO_POSITIVE_RUN = "a positive run this configuration does not have"
"""What the checker double raises when it is asked for a run it cannot have."""

_ROWS_DISAGREE = "the rows disagree about why they are errors"
"""What :func:`mismatch` raises when a matrix's errors name different causes."""

MODEL_TEXT = """\
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


def source(*configurations: Configuration) -> FixedSource:
    """Return a source holding the given configurations.

    Parameters
    ----------
    *configurations : Configuration
        The configurations the matrix runs.

    Returns
    -------
    FixedSource
        The source, over :data:`MODEL_TEXT`.
    """
    return FixedSource(configurations, source=MODEL_TEXT)


def bounded_configuration(
    name: str = "head",
    *,
    max_actions: int | None = 8,
    expectations: tuple[Expectation, ...] = (domain.Holds("S1"),),
) -> Configuration:
    """Build one configuration with an optional action bound.

    Parameters
    ----------
    name : str
        The configuration's name.
    max_actions : int | None
        The exploration bound to declare, or ``None`` to declare none.
    expectations : tuple[Expectation, ...]
        What the configuration expects of its run.

    Returns
    -------
    Configuration
        The configuration.
    """
    options: dict[str, object] = (
        {} if max_actions is None else {"options": {"max_actions": max_actions}}
    )
    return configuration(name, options=options, expectations=expectations)


class Bounded:
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

    def check(self, spec: MaterializedSpec) -> CheckResult:
        """Answer a pass, counting states from the bound it was given.

        Parameters
        ----------
        spec : MaterializedSpec
            The specification to answer.

        Returns
        -------
        CheckResult
            A pass whose count is larger at the doubled bound when the double
            grows.
        """
        self.spec_texts.append(spec.text)
        doubled = "max_actions: 16" in spec.text
        if not doubled:
            return domain.Passed(80, 0.1, ())
        return domain.Passed(160 if self._grows else 80, 0.1, ())


class Refusing:
    """A checker that reports a run which proved nothing."""

    @staticmethod
    def check(spec: MaterializedSpec) -> CheckResult:
        """Answer with an error regardless of the specification.

        Parameters
        ----------
        spec : MaterializedSpec
            The specification this run would have checked.

        Returns
        -------
        CheckResult
            An error, which no fixpoint comparison may read a count from.
        """
        del spec
        return domain.Errored("no verdict line")


class Answers:
    """A checker answering each targeted run from a result held for it.

    Parameters
    ----------
    targeted : dict[str, domain.CheckResult]
        The result the run for each expected failure reports, keyed by the
        assertion that run keeps.
    positive : domain.CheckResult | None
        The result the run holding the assertions expected to hold reports, or
        ``None`` when the configuration expects only defects and so has no
        positive run.
    """

    def __init__(
        self,
        targeted: dict[str, domain.CheckResult],
        *,
        positive: domain.CheckResult | None = None,
    ) -> None:
        """Hold the results and the assertion each is keyed by."""
        self._targeted = targeted
        self._positive = positive

    def check(self, spec: MaterializedSpec) -> CheckResult:
        """Answer the run whose specification still carries an assertion.

        Parameters
        ----------
        spec : MaterializedSpec
            The specification to answer. A targeted run retains only the
            assertions it is judged by, so the assertion present identifies
            which expected failure this run was built for.

        Returns
        -------
        CheckResult
            The result held for that run.

        Raises
        ------
        AssertionError
            If the specification carries no expected failure and the
            configuration has no positive run, which the service never asks.
        """
        for name, result in self._targeted.items():
            if f"assertion {name}:" in spec.text:
                return result
        if self._positive is None:
            raise AssertionError(_NO_POSITIVE_RUN)
        return self._positive


def counterexample(
    name: str, kind: domain.CounterexampleKind, count: int
) -> CheckResult:
    """Return a counterexample of ``kind`` that reported ``count`` states.

    Parameters
    ----------
    name : str
        The assertion the counterexample violates.
    kind : domain.CounterexampleKind
        The shape of the path the counterexample reports.
    count : int
        The number of states the run reported.

    Returns
    -------
    CheckResult
        The counterexample, with the graph's trace left empty because these
        tests are about the counts rather than the path.
    """
    return domain.Counterexample(name, kind, trace(), count, 0.1)


def mismatch(judgements: cabc.Iterable[domain.Judgement]) -> str | None:
    """Return the count-mismatch detail a judgement set carries, if any.

    Parameters
    ----------
    judgements : collections.abc.Iterable[domain.Judgement]
        The judgements one matrix produced.

    Returns
    -------
    str | None
        The one mismatch detail, or ``None`` when the configuration was not
        refused for disagreeing counts. More than one detail would mean the
        rows disagree about why they are errors, which is itself a defect.

    Raises
    ------
    ValueError
        If the rows name more than one count mismatch.
    """
    details = {
        judgement.result.detail
        for judgement in judgements
        if isinstance(judgement.result, domain.Errored)
        and "explored different state counts" in judgement.result.detail
    }
    if len(details) > 1:
        raise ValueError(_ROWS_DISAGREE)
    return details.pop() if details else None
