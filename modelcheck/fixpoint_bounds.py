"""Read and double the action bound a configuration declares.

The fixpoint comparison is only meaningful when the second run really was given
a larger bound than the first. That makes the bound a value the comparison has
to read, validate and rewrite, and each of those steps can refuse: a
configuration with no ``options`` table, or with a ``max_actions`` that is not
an integer, has no bound to double.

The readers live here rather than in :mod:`modelcheck.fixpoint` so the
comparison reads as the loop it is. Nothing here opens a file, starts a process
or reads the clock: a configuration is a value, and so is the bound it
declares.
"""

import collections.abc as cabc
import dataclasses as dc
import typing as typ

from modelcheck.domain import MaterializeError

if typ.TYPE_CHECKING:
    from modelcheck.domain import Configuration

__all__ = ["FIXPOINT_FACTOR", "declared_bound", "doubled"]

_ACTION_BOUND = "max_actions"
"""The frontmatter bound the fixpoint check doubles."""

FIXPOINT_FACTOR = 2
"""What the bound is multiplied by to test whether exploration was truncated."""


def doubled(configuration: Configuration) -> Configuration:
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
        :func:`declared_bound`, since the run would then have no bound the
        check could compare.
    """
    bound = declared_bound(configuration)
    options = {
        **configuration.options,
        "options": {
            **_options(configuration),
            _ACTION_BOUND: bound * FIXPOINT_FACTOR,
        },
    }
    return dc.replace(configuration, options=options)


def declared_bound(configuration: Configuration) -> int:
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
