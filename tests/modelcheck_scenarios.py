"""The model and the state a model-check scenario builds up.

``test_model_check_behaviour.py`` holds the scenario steps: the sentences a
feature writes, and the assertions it makes about a rendered report.
Everything those steps read or write lives here — the model text, the
counterexample kinds a scenario may name, the :class:`Matrix` one scenario
builds and reads back, and the readers a step uses to find a configuration's
row.

The seam is drawn where a step stops describing and starts reading: a step that
adds a configuration calls :func:`declared`, and a step that checks one calls
:func:`row`. A reader following a scenario therefore reads only the sentences,
and a reader changing what a matrix holds reads only this module. The split is
also what keeps the step module within the house line limit.

This module is test support and is linted as production code, following the
convention in ``docs/repository-layout.md``: ``test_*.py`` modules hold tests,
anything else here holds helpers.
"""

import dataclasses
import typing as typ

from modelcheck_doubles import FakeChecker, FixedSource

from modelcheck import domain, report

if typ.TYPE_CHECKING:
    from modelcheck.domain import Configuration, Judgement
    from modelcheck.ports import ModelChecker

DEFAULT_SOURCE = """\
action Init:              # maps: buildlimits.admission.Admission.submit
  n = 0

atomic fair action Bump:  # maps: buildlimits.admission.Admission.token_arrived
  # fairness: F1
  n = n + 1

always assertion S1:
  return n < 10

always assertion S2:
  return n < 20

always eventually assertion L1:
  return n >= 1

always eventually assertion L3:
  return n >= 1

always eventually assertion L4a:
  return n >= 1

exists assertion StopIssued:
  return n == 3
"""
"""A small model declaring every property the scenarios name.

The declarations matter for the known-bad scenarios rather than for the fixed
ones: `materialize` strips every assertion a run does not retain, so a
configuration expecting a defect in `L3` is materialized against a source that
must declare `L3`. A model without it would fail the run for the tooling's
reason rather than the scenario's, which is what the `MaterializeError` this
source replaces was reporting.
"""

KINDS = {"lasso": "LASSO", "finite trace": "FINITE"}
"""The counterexample kinds a scenario may name, and their domain members."""

EFFECTS = "buildlimits.effects"
"""The module declaring the effect types, which a scenario may name bare.

A scenario speaks of "an effect ``Pause``" because that is how a maintainer
reads the production package, where the type is ``effects.Pause``. The
inventory holds the dotted path, so a bare name is qualified here rather than
making every scenario spell out a module it did not mention.
"""


class Matrix:
    """The state one scenario builds and then reads back.

    Keeping it on one object rather than in module globals means a scenario
    cannot see another's, and a step that forgets to set something fails where
    it is read.
    """

    def __init__(self) -> None:
        """Start with an empty matrix and a checker that has not been told."""
        self.configurations: list[Configuration] = []
        self.checker: ModelChecker = FakeChecker()
        self.source = DEFAULT_SOURCE
        self.judgements: tuple[Judgement, ...] = ()
        self.states_override: int | None = None
        self.fairness: dict[str, str] = {}
        self.mapped: frozenset[str] = frozenset()
        self.inventory: frozenset[str] = frozenset()
        self.resolves: dict[str, bool] = {}
        self.drift_report = None

    def source_of(self) -> FixedSource:
        """Return the configuration source this scenario describes.

        Returns
        -------
        FixedSource
            The configurations and the model source, as values.
        """
        return FixedSource(tuple(self.configurations), source=self.source)


def declared(
    name: str,
    *,
    min_states: int = 1,
    expectations: tuple[object, ...] = (),
) -> Configuration:
    """Build one configuration of the matrix under test.

    Parameters
    ----------
    name : str
        The configuration's name.
    min_states : int
        The exploration floor.
    expectations : tuple[object, ...]
        What the configuration expects of its run.

    Returns
    -------
    Configuration
        The configuration those parts describe.

    Examples
    --------
    A configuration that expects one property to hold, over the default floor:

    >>> config = declared("head", expectations=(domain.Holds("S1"),))
    >>> config.name, config.min_states, config.expectations
    ('head', 1, (Holds(property_name='S1'),))
    """
    return domain.Configuration(
        name,
        {},
        {},
        {},
        min_states,
        typ.cast("tuple[domain.Expectation, ...]", expectations),
    )


def reconfigured(matrix: Matrix) -> FixedSource:
    """Return the source with the scenario's fairness carried by every run.

    ``materialize`` writes a configuration's fairness levels into the actions
    whose ``# fairness:`` tag it names, so a scenario that edited only the
    source would leave the run carrying no fairness to quote. Both carry the
    tags, as a real ``configurations.toml`` does.

    Parameters
    ----------
    matrix : Matrix
        The scenario state.

    Returns
    -------
    FixedSource
        The source, with every configuration restated to carry the tags.
    """
    if not matrix.fairness:
        return matrix.source_of()
    levelled = tuple(
        dataclasses.replace(configuration, fairness=matrix.fairness)
        for configuration in matrix.configurations
    )
    return FixedSource(levelled, source=matrix.source)


def expected_failure(matrix: Matrix) -> str | None:
    """Return the property the scenario expects a counterexample of.

    Parameters
    ----------
    matrix : Matrix
        The scenario state, whose configurations have been added by earlier
        steps.

    Returns
    -------
    str | None
        The property named by the scenario's sole ``FailsAs`` expectation, or
        ``None`` when the scenario expects no counterexample at all. A recording
        is a shape captured from another model, so it can only stand in for a
        run of this scenario's model if the assertion it names is the one this
        scenario declared; see :class:`modelcheck_doubles.FakeChecker`.
    """
    named = {
        expectation.property_name
        for configuration in matrix.configurations
        for expectation in configuration.expectations
        if isinstance(expectation, domain.FailsAs)
    }
    return next(iter(named)) if len(named) == 1 else None


def qualified(name: str) -> str:
    """Return the dotted path a scenario's name stands for.

    Parameters
    ----------
    name : str
        A bare effect name, or an already-qualified path.

    Returns
    -------
    str
        The qualified path.

    Examples
    --------
    >>> qualified("Pause"), qualified("buildlimits.admission.Admission.submit")
    ('buildlimits.effects.Pause', 'buildlimits.admission.Admission.submit')
    """
    return name if "." in name else f"{EFFECTS}.{name}"


def row(matrix: Matrix, configuration: str, subject: str) -> str:
    """Return the rendered report line naming a configuration and a subject.

    Parameters
    ----------
    matrix : Matrix
        The scenario state holding the judgements.
    configuration : str
        The configuration the line must name.
    subject : str
        The property or probe the line must name.

    Returns
    -------
    str
        The first matching line.

    Raises
    ------
    AssertionError
        If no line names both: the report is missing a row the scenario needs.
    """
    rendered = report.render_report(matrix.judgements)
    matches = [
        line
        for line in rendered.splitlines()
        if configuration in line and subject in line
    ]
    if not matches:
        msg = f"no line names {configuration} and {subject}:\n{rendered}"
        raise AssertionError(msg)
    return matches[0]
