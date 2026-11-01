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
    MaterializeError
        If the model declares no configurations, a configuration declares no
        ``max_actions`` bound to double, or a run that should pass does not.

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
    return tuple(
        _compared(text, configuration, checker)
        for configuration in configurations
        if not only_failures(configuration)
    )


def _compared(
    text: str, configuration: Configuration, checker: ModelChecker
) -> Fixpoint:
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
    Fixpoint
        The two state counts.

    Raises
    ------
    MaterializeError
        If the configuration declares no ``max_actions`` bound or a run that
        should pass does not.
    """  # ruff: ignore[docstring-extraneous-exception] - _doubled and _pass_run raise it.
    declared = _pass_run(text, configuration, checker)
    doubled = _pass_run(text, _doubled(configuration), checker)
    return Fixpoint(configuration.name, declared.states, doubled.states)


def _pass_run(text: str, configuration: Configuration, checker: ModelChecker) -> Passed:
    """Materialize one configuration and require a pass.

    Parameters
    ----------
    text : str
        The model source.
    configuration : Configuration
        The variant to run.
    checker : ModelChecker
        The checker the run is delegated to.

    Returns
    -------
    Passed
        The pass the run produced.

    Raises
    ------
    MaterializeError
        If the run did not pass, because its state count is then not a count
        of the model's states and comparing it would prove nothing.
    """
    spec = materialize(text, configuration, expected_failure=None)
    result = checker.check(spec)
    if not isinstance(result, Passed):
        msg = (
            f"configuration {configuration.name!r} did not pass at the bound "
            f"the fixpoint check needs, so its state count cannot be compared"
        )
        raise MaterializeError(msg)
    return result


def _doubled(configuration: Configuration) -> Configuration:
    """Return the configuration with its ``max_actions`` bound doubled.

    Parameters
    ----------
    configuration : Configuration
        The variant to double.

    Returns
    -------
    Configuration
        The same variant with twice the exploration bound.

    Raises
    ------
    MaterializeError
        If the frontmatter declares no integer ``max_actions``, since the
        run would then have no bound the check could compare.
    """
    bounds = configuration.options.get("options")
    bound = bounds.get(_ACTION_BOUND) if isinstance(bounds, cabc.Mapping) else None
    if not isinstance(bounds, cabc.Mapping) or not _is_int(bound):
        msg = (
            f"configuration {configuration.name!r} declares no integer "
            f"{_ACTION_BOUND!r} to double"
        )
        raise MaterializeError(msg)
    options = {
        **configuration.options,
        "options": {**bounds, _ACTION_BOUND: bound * _FIXPOINT_FACTOR},
    }
    return dc.replace(configuration, options=options)


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
