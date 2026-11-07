"""Prove that a run's state count is the model's, not its bound's.

FizzBee truncates exploration at ``max_actions`` without printing a marker, so
a run that exhausted its bound can still report a pass. The documented test is
that doubling the bound leaves the state count unchanged: an exploration that
was cut short grows, one that reached its fixpoint does not.

Only configurations with a run that should pass are compared. A mutant's
declared run is expected to stop at its counterexample, so its state count says
nothing about the fixed model's exploration; the fixed run is the one the
``min_states`` floor and this check are about.

Nothing here opens a file, starts a process or reads the clock. The model text,
the configurations and the checker all arrive through the ports, which is what
lets the comparison be exercised against a recording.
"""

import dataclasses as dc
import typing as typ

from modelcheck.domain import Errored, MaterializeError, Passed
from modelcheck.expectations import only_failures
from modelcheck.fixpoint_bounds import FIXPOINT_FACTOR, declared_bound, doubled
from modelcheck.fixpoint_failures import FixpointError, FixpointFailure, Stage
from modelcheck.materialize import materialize

if typ.TYPE_CHECKING:
    from modelcheck.domain import Configuration
    from modelcheck.ports import ConfigurationSource, ModelChecker


@dc.dataclass(frozen=True, slots=True)
class Fixpoint:
    """One configuration's exploration at its declared and doubled bound.

    Both runs are retained rather than only their counts. A comparison that
    passed is evidence about the bound, and the artefact has to say which bound
    it was and how long each run took, or a reader cannot tell a fast traversal
    of a small bound from a slow one of a large bound. The counts stay
    available as properties so a caller reads the comparison, not the runs.

    Attributes
    ----------
    configuration : str
        The configuration compared.
    bound : int
        The ``max_actions`` bound the declared run was given.
    declared : Passed
        The run at the configuration's own bound.
    doubled : Passed
        The run at twice that bound.
    """

    configuration: str
    bound: int
    declared: Passed
    doubled: Passed

    @property
    def states(self) -> int:
        """The state count the declared run explored."""
        return self.declared.states

    @property
    def doubled_states(self) -> int:
        """The state count the doubled run explored."""
        return self.doubled.states

    @property
    def doubled_bound(self) -> int:
        """The bound the doubled run was given."""
        return self.bound * FIXPOINT_FACTOR

    @property
    def is_stable(self) -> bool:
        """Report whether doubling the bound left the state count unchanged.

        Returns
        -------
        bool
            Whether the two runs enumerated the same number of states.

        Examples
        --------
        >>> stable = Fixpoint("head", 10, Passed(10, 0.5, ()), Passed(10, 0.9, ()))
        >>> grown = Fixpoint("head", 10, Passed(10, 0.5, ()), Passed(20, 0.9, ()))
        >>> stable.is_stable, grown.is_stable
        (True, False)
        """
        return self.states == self.doubled_states


def run_fixpoint(
    source: ConfigurationSource, checker: ModelChecker
) -> tuple[Fixpoint, ...]:
    r"""Compare every configuration's exploration at two action bounds.

    Parameters
    ----------
    source : ConfigurationSource
        The model text and the configurations to compare.
    checker : ModelChecker
        The checker every run is delegated to.

    Returns
    -------
    tuple[Fixpoint, ...]
        One comparison per configuration that should pass, in configuration
        order.

    Raises
    ------
    FixpointError
        If a run that should pass does not, or a configuration cannot be
        materialized. The error carries every failed comparison's
        configuration, bound, result and diagnostic, so a caller can save them
        before reporting the failure. A configuration that cannot be
        materialized is recorded as a failure rather than raised on its own, so
        it cannot discard the evidence of a comparison that already failed.
    MaterializeError
        If the model declares no configurations to compare.

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
    ...         options = {"options": {"max_actions": 8}}
    ...         return (
    ...             domain.Configuration(
    ...                 "head", {}, {}, options, 1, (domain.Holds("S1"),)
    ...             ),
    ...         )
    ...     def exclusions(self):
    ...         return {}
    >>> class Checker:
    ...     def check(self, spec):
    ...         return domain.Passed(4, 0.5, ())
    >>> [found.is_stable for found in run_fixpoint(Source(), Checker())]
    [True]
    """
    text = source.source_text()
    configurations = source.configurations()
    if not configurations:
        msg = "the model declares no configurations to compare"
        raise MaterializeError(msg)
    # Every comparable configuration is attempted before the refusal, so one
    # failing comparison does not hide another's evidence. A configuration the
    # materializer refuses is caught here for the same reason: it is evidence
    # about that configuration, not a reason to discard the comparisons that
    # already ran.
    comparisons: list[Fixpoint] = []
    failures: list[FixpointFailure] = []
    for configuration in configurations:
        if only_failures(configuration):
            continue
        try:
            found, failed = _compared(text, configuration, checker)
        except MaterializeError as error:
            failures.append(_materialization_failure(configuration, error))
            continue
        if found is not None:
            comparisons.append(found)
        failures.extend(failed)
    if failures:
        raise FixpointError(tuple(comparisons), tuple(failures))
    return tuple(comparisons)


def _materialization_failure(
    configuration: Configuration, error: MaterializeError
) -> FixpointFailure:
    """Return the failure of a configuration that could not be built.

    Parameters
    ----------
    configuration : Configuration
        The variant the materializer refused.
    error : MaterializeError
        The materializer's refusal, whose message names what the configuration
        lacks.

    Returns
    -------
    FixpointFailure
        A failure at the ``materialize`` stage with no bound and no fairness in
        force, because neither was ever reached. The refusal is carried as an
        :class:`~modelcheck.domain.Errored` result so the artefact records it
        the same way as a run that proved nothing.
    """
    return FixpointFailure(
        configuration.name,
        "materialize",
        None,
        (),
        Errored(str(error)),
    )


def _compared(
    text: str, configuration: Configuration, checker: ModelChecker
) -> tuple[Fixpoint | None, tuple[FixpointFailure, ...]]:
    """Run one configuration at its bound and at twice its bound.

    Parameters
    ----------
    text : str
        The model source.
    configuration : Configuration
        The variant to compare.
    checker : ModelChecker
        The checker every run is delegated to.

    Returns
    -------
    tuple[Fixpoint | None, tuple[FixpointFailure, ...]]
        The two state counts when both runs passed, or the failure of the
        first run that did not. A failed declared-bound run is not followed by
        a doubled-bound one: the first already proves the count cannot be
        compared, and repeating it at a larger bound only spends the time.

    Raises
    ------
    MaterializeError
        If the configuration declares no ``max_actions`` bound. The bound is
        read before the first run, so a configuration the comparison cannot
        use never reaches the checker: its result would have been discarded
        and the run's time spent proving nothing.
    """  # ruff: ignore[docstring-extraneous-exception] - declared_bound raises it.
    bound = declared_bound(configuration)
    declared = _pass_run(text, configuration, "declared", checker)
    match declared:
        case FixpointFailure():
            return None, (declared,)
    doubled_run = _pass_run(text, doubled(configuration), "doubled", checker)
    match doubled_run:
        case FixpointFailure():
            # The declared run did pass, and its count is the number the
            # comparison was going to check the doubled run against. Keeping
            # it in the failure is what lets the record say how far the model
            # got rather than only that a later run failed.
            return None, (dc.replace(doubled_run, declared=declared),)
    return (Fixpoint(configuration.name, bound, declared, doubled_run), ())


def _pass_run(
    text: str,
    configuration: Configuration,
    stage: Stage,
    checker: ModelChecker,
) -> Passed | FixpointFailure:
    """Materialize one configuration, requiring a pass or recording why not.

    Parameters
    ----------
    text : str
        The model source.
    configuration : Configuration
        The variant to run.
    stage : Stage
        Which of the two comparison runs this is.
    checker : ModelChecker
        The checker the run is delegated to.

    Returns
    -------
    Passed | FixpointFailure
        The pass the run produced, or the evidence of a run that did not pass.
        A run that did not pass keeps its result rather than being discarded:
        its state count is not a count of the model's states, but its
        diagnostic or counterexample is exactly what a reader needs.
    """
    spec = materialize(text, configuration, expected_failure=None)
    result = checker.check(spec)
    match result:
        case Passed():
            return result
        case _:
            return FixpointFailure(
                configuration.name,
                stage,
                declared_bound(configuration),
                spec.fairness_in_force,
                result,
            )
