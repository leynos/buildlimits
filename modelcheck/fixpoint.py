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

import collections.abc as cabc
import dataclasses as dc
import typing as typ

from modelcheck.domain import MaterializeError, Passed
from modelcheck.expectations import only_failures
from modelcheck.fixpoint_failures import FixpointError, FixpointFailure, Stage
from modelcheck.materialize import materialize

if typ.TYPE_CHECKING:
    from modelcheck.domain import Configuration
    from modelcheck.ports import ConfigurationSource, ModelChecker

_ACTION_BOUND = "max_actions"
"""The frontmatter bound the fixpoint check doubles."""

_FIXPOINT_FACTOR = 2
"""What the bound is multiplied by to test whether exploration was truncated."""


@dc.dataclass(frozen=True, slots=True)
class Fixpoint:
    """One configuration's state count at its declared and doubled bound.

    Attributes
    ----------
    configuration : str
        The configuration compared.
    states : int
        Distinct states explored at the declared bound.
    doubled_states : int
        Distinct states explored at twice that bound.
    """

    configuration: str
    states: int
    doubled_states: int

    @property
    def is_stable(self) -> bool:
        """Report whether doubling the bound left the state count unchanged.

        Returns
        -------
        bool
            Whether the two runs enumerated the same number of states.

        Examples
        --------
        >>> Fixpoint("head", 10, 10).is_stable, Fixpoint("head", 10, 20).is_stable
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
        If a run that should pass does not. The error carries every failed
        comparison's configuration, bound, result and diagnostic, so a caller
        can save them before reporting the failure.
    MaterializeError
        If the model declares no configurations or a configuration declares no
        ``max_actions`` bound to double.

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
    # failing comparison does not hide another's evidence.
    compared = [
        _compared(text, configuration, checker)
        for configuration in configurations
        if not only_failures(configuration)
    ]
    comparisons = tuple(found for found, _ in compared if found is not None)
    failures = tuple(failure for _, found in compared for failure in found)
    if failures:
        raise FixpointError(comparisons, failures)
    return comparisons


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
        If the configuration declares no ``max_actions`` bound.
    """  # ruff: ignore[docstring-extraneous-exception] - _doubled raises it.
    declared = _pass_run(text, configuration, "declared", checker)
    if isinstance(declared, FixpointFailure):
        return None, (declared,)
    doubled = _pass_run(text, _doubled(configuration), "doubled", checker)
    if isinstance(doubled, FixpointFailure):
        return None, (doubled,)
    return Fixpoint(configuration.name, declared.states, doubled.states), ()


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
    if isinstance(result, Passed):
        return result
    return FixpointFailure(
        configuration.name,
        stage,
        _declared_bound(configuration),
        spec.fairness_in_force,
        result,
    )


def _doubled(configuration: Configuration) -> Configuration:
    """Return the configuration with its ``max_actions`` bound doubled.

    Parameters
    ----------
    configuration : Configuration
        The variant to double.

    Returns
    -------
    Configuration
        The same variant with twice the exploration bound. A configuration
        that declares no integer ``max_actions`` is refused by
        :func:`_declared_bound`, since the run would then have no bound the
        check could compare.
    """
    bound = _declared_bound(configuration)
    options = {
        **configuration.options,
        "options": {
            **_options(configuration),
            _ACTION_BOUND: bound * _FIXPOINT_FACTOR,
        },
    }
    return dc.replace(configuration, options=options)


def _declared_bound(configuration: Configuration) -> int:
    """Return the integer ``max_actions`` bound a configuration declares.

    Parameters
    ----------
    configuration : Configuration
        The variant to read.

    Returns
    -------
    int
        The bound the run is given, and the number the comparison doubles.

    Raises
    ------
    MaterializeError
        If the frontmatter declares no integer ``max_actions``, since the run
        would then have no bound the check could compare.
    """
    bound = _options(configuration).get(_ACTION_BOUND)
    if not _is_int(bound):
        raise MaterializeError(_missing_bound_message(configuration))
    return bound


def _options(configuration: Configuration) -> cabc.Mapping[str, object]:
    """Return the frontmatter's ``options`` table.

    Parameters
    ----------
    configuration : Configuration
        The variant to read.

    Returns
    -------
    collections.abc.Mapping[str, object]
        The table the bound is declared in.

    Raises
    ------
    MaterializeError
        If the frontmatter has no ``options`` table, so no bound could be
        declared at all.
    """
    bounds = configuration.options.get("options")
    if not isinstance(bounds, cabc.Mapping):
        raise MaterializeError(_missing_bound_message(configuration))
    return bounds


def _missing_bound_message(configuration: Configuration) -> str:
    """Return the refusal for a configuration with no usable action bound.

    The message is built here so the two readers that can find no bound refuse
    in the same words. The caller raises :class:`MaterializeError` itself, so
    each docstring names the exception it really raises.

    Parameters
    ----------
    configuration : Configuration
        The variant that declares none.

    Returns
    -------
    str
        The refusal, naming the configuration and the key it lacks.
    """
    return (
        f"configuration {configuration.name!r} declares no integer "
        f"{_ACTION_BOUND!r} to double"
    )


def _is_int(value: object) -> typ.TypeGuard[int]:
    """Report whether a decoded TOML value is a genuine integer bound.

    ``bool`` is a subclass of ``int`` in Python, so ``max_actions = true``
    would otherwise be doubled to ``2`` and compared as a bound. The check is
    a query so the refusal above reads as one condition, and the guard tells a
    type checker what the refusal has already established: the value being
    doubled is an ``int``, not an opaque decoded TOML value.

    Parameters
    ----------
    value : object
        The decoded value to classify.

    Returns
    -------
    typing.TypeGuard[int]
        Whether the value is an integer and not a boolean.

    Examples
    --------
    >>> _is_int(8), _is_int(True), _is_int("8")
    (True, False, False)
    """
    return isinstance(value, int) and not isinstance(value, bool)
