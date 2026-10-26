"""The app, its model and its configurations, built once for the CLI tests.

Two command-line test modules need the same three things: a model source that
declares every assertion a configuration may name, a builder for configurations
that expect one thing, and an app assembled from adapter factories. Writing them
out twice would make two copies drift apart silently, and a change to
``make_app`` would have to be made in both.

This module is test support and is linted as production code, following the
convention in ``docs/repository-layout.md``: ``test_*.py`` modules hold tests,
anything else here holds helpers.
"""

import typing as typ

from modelcheck_doubles import FixedSource

from modelcheck import cli, domain
from modelcheck.ports import CodeInventory

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import cyclopts

SOURCE = """\
action Init:  # maps: buildlimits.admission.Admission.submit
  n = 0

always assertion S1:
  return n < 10

always assertion S2:
  return n < 20

always eventually assertion L3:
  return n >= 1

exists assertion StopIssued:
  return n == 3
"""
"""A model mapping one path, and declaring every assertion the tests name.

The assertions are here rather than in each test because ``materialize`` retains
only the assertions a run is about and raises when one of them is missing: a
known-bad configuration that names ``L3`` cannot be materialized against a
source that never declares it. The mapping is what the drift command reads.
"""


class Inventory(CodeInventory):
    """A stand-in for the production inventory.

    It subclasses :class:`~modelcheck.ports.CodeInventory` rather than merely
    satisfying it, so each method is an override of a declared port method.
    The adapter's own methods are pure forwarding by design — the port exists
    so the service can be handed something other than the production reader —
    and saying so in the type system is what distinguishes that deliberate
    indirection from an accidental one.

    Parameters
    ----------
    items : frozenset[str]
        The dotted paths the inventory holds.
    resolves : collections.abc.Callable[[str], bool]
        How the inventory answers a resolution question. The default says every
        path it holds resolves, which is what an inventory reads as when the
        production package still declares it.
    """

    def __init__(
        self,
        items: frozenset[str] = frozenset(),
        resolves: cabc.Callable[[str], bool] = lambda _path: True,
    ) -> None:
        """Hold the paths and the resolution rule."""
        self._items = items
        self._resolves = resolves

    @typ.override
    def items(self) -> frozenset[str]:
        """Return the inventory's paths.

        Returns
        -------
        frozenset[str]
            The dotted paths the production package declares.
        """
        return self._items

    @typ.override
    def resolves(self, dotted_path: str) -> bool:
        """Report whether a path still resolves.

        Parameters
        ----------
        dotted_path : str
            The path to test.

        Returns
        -------
        bool
            The rule's answer.
        """
        return self._resolves(dotted_path)


def configuration(
    name: str,
    expectations: tuple[domain.Expectation, ...],
    *,
    min_states: int = 1,
) -> domain.Configuration:
    """Build one configuration with the given expectations.

    Parameters
    ----------
    name : str
        The configuration's name.
    expectations : tuple[domain.Expectation, ...]
        What it expects of its run.
    min_states : int
        The exploration floor.

    Returns
    -------
    domain.Configuration
        The configuration.
    """
    return domain.Configuration(name, {}, {}, {}, min_states, expectations)


def holds(name: str = "head", property_name: str = "S1") -> domain.Configuration:
    """Build a configuration that expects a property to hold.

    Parameters
    ----------
    name : str
        The configuration's name.
    property_name : str
        The property it expects to hold.

    Returns
    -------
    domain.Configuration
        The configuration.
    """
    return configuration(name, (domain.Holds(property_name),))


def fails_as(name: str, property_name: str) -> domain.Configuration:
    """Build a known-bad configuration expecting a lasso.

    Parameters
    ----------
    name : str
        The configuration's name.
    property_name : str
        The property it expects to fail.

    Returns
    -------
    domain.Configuration
        The configuration.
    """
    return configuration(
        name,
        (domain.FailsAs(property_name, domain.CounterexampleKind.LASSO),),
    )


def app(
    configurations: cabc.Sequence[domain.Configuration],
    checker: object,
    *,
    inventory: frozenset[str] = frozenset(),
    resolves: cabc.Callable[[str], bool] = lambda _path: True,
) -> cyclopts.App:
    """Build the app over a fixed source, a checker double and an inventory.

    Parameters
    ----------
    configurations : collections.abc.Sequence[Configuration]
        The configurations the matrix runs.
    checker : object
        The checker double.
    inventory : frozenset[str]
        The production paths the drift check compares against.
    resolves : collections.abc.Callable[[str], bool]
        How the inventory answers a resolution question.

    Returns
    -------
    cyclopts.App
        The application under test.
    """
    source = FixedSource(configurations, source=SOURCE)
    return cli.make_app(
        lambda _path: checker,
        lambda _directory, _bounds: source,
        lambda: Inventory(inventory, resolves),
    )
