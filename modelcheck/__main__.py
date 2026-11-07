"""Run the model check as a command: wiring, and nothing else.

This module is the one place that constructs the real adapters. Every other
module receives them through a port, which is what lets the whole matrix and
the whole command line be exercised without a binary or a checkout: the tests
build the app with :func:`modelcheck.cli.make_app` and hand it doubles.

Keeping the construction here also keeps the imports measurable. Importing
:mod:`modelcheck.adapters.fizzbee_process` costs nothing at module scope, but
importing ``buildlimits`` does, and a test that imported this module would pay
for both. The app is built inside :func:`main`, and ``main`` runs only under
``__main__``, so pytest's ``--doctest-modules`` collection imports this module
without running anything.

The status mapping is the same three-way split the command line documents:
``0`` met, ``1`` unmet, ``2`` the tool could not run. A usage error and a
defect in the model's own configuration both mean the checker was never asked
about the model, so both leave through ``2`` rather than being reported as a
failing expectation.
"""

import sys
import typing as typ

import cyclopts

from modelcheck import cli
from modelcheck.domain import MaterializeError

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """Run the command line and return its exit status.

    Parameters
    ----------
    argv : list[str] | None
        The arguments to parse, or ``None`` to read ``sys.argv``.

    Returns
    -------
    int
        ``0`` when every expectation was met, ``1`` when any was not, and ``2``
        when the tool could not run — including a usage error, which Cyclopts
        raises because the app is built with ``exit_on_error=False``.

    Notes
    -----
    The adapters are imported inside the function rather than at module scope
    so that collecting this module's doctests costs nothing and so that a
    missing optional dependency fails at the point of use, where the message
    can name the command that needed it.
    """
    environment = _adapters()
    app = cli.make_app(
        environment.checker,
        environment.configurations,
        environment.inventory,
    )
    try:
        status = app(argv)
    except (cyclopts.CycloptsError, MaterializeError, OSError, ValueError) as error:
        print(f"modelcheck: {error}", file=sys.stderr)
        return cli.EXIT_TOOL_FAILURE
    return cli.EXIT_MET if status is None else status


class _Adapters(typ.NamedTuple):
    """The three factories the command line is built with.

    Attributes
    ----------
    checker : modelcheck.cli.CheckerFactory
        Builds the process adapter from ``--fizz``.
    configurations : modelcheck.cli.SourceFactory
        Builds the TOML source from the model directory and the bound set.
    inventory : modelcheck.cli.InventoryFactory
        Builds the production inventory.
    """

    checker: cli.CheckerFactory
    configurations: cli.SourceFactory
    inventory: cli.InventoryFactory


def _adapters() -> _Adapters:
    """Return the real adapters, imported at the point of use.

    Returns
    -------
    _Adapters
        One factory per boundary. Each is a factory rather than a value because
        the checker is built from a path the command line supplies, and the
        source is built from the model directory the command line supplies.
    """
    from modelcheck.adapters.configuration import TomlConfigurations
    from modelcheck.adapters.fizzbee_process import FizzbeeChecker
    from modelcheck.adapters.inventory import BuildlimitsInventory

    return _Adapters(
        checker=FizzbeeChecker,
        configurations=TomlConfigurations,
        inventory=BuildlimitsInventory,
    )


if __name__ == "__main__":
    sys.exit(main())
