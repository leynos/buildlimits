"""The drift command's failures are the tool's, not the model's.

``drift`` compares values it is handed: the mappings a source reads from the
model, and the surface an inventory enumerates from the production package.
Neither comparison runs when a port raises, and the status must say so. ``1``
is reserved for a comparison that ran and found a difference; reporting a
source that could not be read, or a package that could not be imported, as a
drift finding would tell CI the model had changed when nothing was compared.

The command is exercised through the real entry point rather than by calling
the comparison directly, because the status and the printed reason are what a
CI job consumes, and both are decided by the wiring this module tests.
"""

import typing as typ

import pytest
from modelcheck_cli_cases import SOURCE, Inventory
from modelcheck_doubles import FakeChecker
from modelcheck_source_doubles import FixedSource

from modelcheck import cli, domain


class _Refusing:
    """A port double that raises the failure it was given.

    Holding one exception and raising it from the method under test is the
    whole of both doubles below, so it is written once. Each subclass still
    names the port it stands in for, because that is what makes its override
    deliberate rather than incidental. The base comes first in each class's
    bases so that its ``super().__init__()`` reaches the port's own
    constructor.
    """

    def __init__(self, error: Exception) -> None:
        """Hold the failure the port's method raises.

        Parameters
        ----------
        error : Exception
            The failure the port is declared to raise.
        """
        super().__init__()
        self.error = error


class _FailingSource(_Refusing, FixedSource):
    """A source that cannot produce its model text, whatever the reason.

    The port's contract says a ``ConfigurationSource`` raises
    :class:`~modelcheck.domain.MaterializeError` for a model it cannot
    represent and :class:`OSError` for a directory it cannot read. Both reach
    the command line as "the tool could not run", so the same double serves
    both cases and the error it carries is the only difference.
    """

    @typ.override
    def source_text(self) -> str:
        """Raise the held failure, as a refused model would."""
        raise self.error


class _FailingInventory(_Refusing, Inventory):
    """An inventory whose production surface cannot be enumerated."""

    @typ.override
    def items(self) -> frozenset[str]:
        """Raise the held failure, as a broken import would."""
        raise self.error


def _drift_through(
    monkeypatch: pytest.MonkeyPatch,
    *,
    configurations: cli.SourceFactory,
    inventory: cli.InventoryFactory,
) -> int:
    """Run ``drift`` through the real entry point with the given adapters.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Installs the factories in place of the real adapters.
    configurations : modelcheck.cli.SourceFactory
        The source the command is given.
    inventory : modelcheck.cli.InventoryFactory
        The inventory the command is given.

    Returns
    -------
    int
        The status the entry point returned.
    """
    from modelcheck import __main__ as entry

    monkeypatch.setattr(
        entry,
        "_adapters",
        lambda: entry._Adapters(
            checker=lambda _path: FakeChecker("pass"),
            configurations=configurations,
            inventory=inventory,
        ),
    )
    return entry.main(["drift", "."])


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(domain.MaterializeError("the model has no source"), id="refused"),
        pytest.param(OSError("the model directory cannot be read"), id="unreadable"),
    ],
)
def test_a_source_the_port_cannot_answer_exits_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    """A source failure is the tool failing, not a drift finding.

    The drift command compares values it was handed; a source that cannot
    produce them means the comparison never ran. Reporting that as ``1`` would
    tell CI the model had drifted when nothing was compared, so both exception
    families the port declares leave through ``2`` with the reason named.
    """
    failing = _FailingSource(error)

    status = _drift_through(
        monkeypatch,
        configurations=lambda _directory, _bounds: failing,
        inventory=Inventory,
    )

    assert status == cli.EXIT_TOOL_FAILURE, "nothing was compared"
    assert "modelcheck:" in capsys.readouterr().err, "and the reason is reported"


def test_an_inventory_the_port_cannot_build_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An inventory that cannot be read is a failure to run, not a clean model.

    Reading the production package is the inventory's job, and a package that
    no longer declares the names it reads is a defect in the tooling. Leaving
    through ``1`` would make that indistinguishable from a model that genuinely
    failed to map the surface.
    """
    inventory = _FailingInventory(domain.MaterializeError("the package is gone"))

    status = _drift_through(
        monkeypatch,
        configurations=lambda _directory, _bounds: FixedSource((), source=SOURCE),
        inventory=lambda: inventory,
    )

    assert status == cli.EXIT_TOOL_FAILURE, "the comparison never ran"
    assert "modelcheck:" in capsys.readouterr().err, "and the reason is reported"
