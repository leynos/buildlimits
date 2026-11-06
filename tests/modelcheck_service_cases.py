"""The model, the source and the checker doubles the service tests share.

``test_modelcheck_service.py`` asks what the matrix does with a configuration's
runs; ``test_modelcheck_fixpoint_service.py`` asks what the comparison does
when the same configuration is run again at a doubled bound. Both need one
model text declaring the assertions they name, a source built from the
configurations under test, and a configuration that declares an exploration
bound, so those live here rather than in either test module.

The checkers are small doubles that answer from the specification they are
handed rather than recordings: these are tests about the comparisons the
service makes, not about what a real run prints.

This module is test support and is linted as production code, following the
convention in ``docs/repository-layout.md``: ``test_*.py`` modules hold tests,
anything else here holds helpers.
"""

import typing as typ

from modelcheck_cases import configuration
from modelcheck_source_doubles import FixedSource

from modelcheck import domain

if typ.TYPE_CHECKING:
    from modelcheck.domain import (
        CheckResult,
        Configuration,
        Expectation,
        MaterializedSpec,
    )

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
