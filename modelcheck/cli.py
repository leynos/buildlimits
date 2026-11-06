"""The command line a maintainer drives the model check with.

Two commands, one per question. ``check`` runs the matrix of configurations and
prints one line per expectation; ``drift`` compares the model's ``# maps:``
annotations with the production surface. Each command is a thin shell over the
service and the report renderers: it parses arguments, builds the adapters the
caller injected, and turns the result into an exit status.

The exit status is the contract the matrix is wired into:

- ``0`` when every expectation was met;
- ``1`` when any expectation was unmet;
- ``2`` when the tool itself could not run — a usage error, a configuration
  the model does not support, or an input that could not be read.

That third status matters as much as the first two. A CI job that treated a
crash as a pass would report green for a model that never ran, so a failure to
start is never folded into ``1``: ``1`` means "the model or the code is wrong",
and ``2`` means "the checker was not asked".

Configuration is environment-first, as the repository's scripting standards
require: ``MODELCHECK_FIZZ``, ``MODELCHECK_BOUNDS`` and the rest fill in any
option the command line does not give, and an explicit flag wins over the
environment. The prefix is registered on the app rather than on each command so
a new option inherits it.

The app is built by :func:`make_app` rather than imported as a module-level
singleton, because the adapters are values the caller supplies. ``__main__`` is
the only place that constructs the real ones, which keeps every test able to
hand the app a stub checker and a fixed source.
"""

import typing as typ
from pathlib import Path

import cyclopts
import cyclopts.config

from modelcheck import drift, report, service
from modelcheck.cli_outputs import Artefact, Outputs, Traces
from modelcheck.fixpoint import FixpointError, run_fixpoint
from modelcheck.selection import select

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from modelcheck.domain import Judgement
    from modelcheck.ports import CodeInventory, ConfigurationSource, ModelChecker

__all__ = ["exit_status", "make_app"]

EXIT_MET = 0
"""Every expectation was met."""

EXIT_UNMET = 1
"""At least one expectation was unmet."""

EXIT_TOOL_FAILURE = 2
"""The tool could not run, so the run says nothing about the model."""

type _BOUNDS = typ.Literal["routine", "nightly"]
"""The two bound sets a run may ask for.

The numbers behind each name belong to the configuration the model ships, not
to this module: ``fizz.yaml`` declares them, and ``--bounds`` only selects
which set is read. Naming them here would put a second copy of a bound in a
file the model never sees.
"""

_ENVIRONMENT = cyclopts.config.Env("MODELCHECK_", command=False)
"""Fill any option the command line omits from ``MODELCHECK_*``.

``command=False`` is what keeps ``MODELCHECK_CHECK`` or ``MODELCHECK_DRIFT``
from being read as an env-backed subcommand name; only the options of the
chosen command are filled.
"""

type CheckerFactory = cabc.Callable[[Path], typ.Any]
"""Build the checker a ``check`` run drives, from the binary's path."""

type SourceFactory = cabc.Callable[[Path, str], ConfigurationSource]
"""Build the configuration source for a model directory and a bound set."""

type InventoryFactory = cabc.Callable[[], CodeInventory]
"""Build the production inventory the drift check compares against."""


def exit_status(judgements: cabc.Sequence[Judgement]) -> int:
    """Return the status a matrix's judgements exit with.

    Parameters
    ----------
    judgements : collections.abc.Sequence[Judgement]
        The judgements the matrix produced.

    Returns
    -------
    int
        ``0`` when every judgement is met and ``1`` when any is not. An empty
        sequence exits ``0``: a matrix with no expectations to fail has nothing
        unmet, and the *configuration* that produced it is already refused by
        the service, which raises rather than returning nothing.

    Examples
    --------
    >>> from modelcheck import domain
    >>> met = domain.Judgement(
    ...     "head", domain.Holds("S1"), domain.Passed(4, 0.1, ()),
    ...     domain.Verdict.HOLDS, (),
    ... )
    >>> unmet = domain.Judgement(
    ...     "head", domain.Holds("S1"), domain.Passed(4, 0.1, ()),
    ...     domain.Verdict.UNEXPECTED, (),
    ... )
    >>> exit_status([met]), exit_status([met, unmet]), exit_status([])
    (0, 1, 0)
    """
    return EXIT_MET if all(judgement.is_met for judgement in judgements) else EXIT_UNMET


def make_app(  # pylint: disable=too-many-arguments  # ADR 008 supplies three adapters.
    checker_factory: CheckerFactory,
    source_factory: SourceFactory,
    inventory_factory: InventoryFactory,
) -> cyclopts.App:
    """Build the application, with its adapters supplied as factories.

    Parameters
    ----------
    checker_factory : CheckerFactory
        Builds the checker from the path ``--fizz`` names.
    source_factory : SourceFactory
        Builds the configuration source for a model directory and a bound set.
    inventory_factory : InventoryFactory
        Builds the production inventory the drift check reads.

    Returns
    -------
    cyclopts.App
        The application. Calling it returns the command's integer status
        rather than exiting, so a test can assert on the status itself.

    Notes
    -----
    The app is built with ``exit_on_error=False``, ``help_on_error=False`` and
    ``print_error=False``, so a usage error raises rather than ending the
    process, and the caller decides what to print. That is what lets the same
    status mapping serve the console entry point and the tests, and it is why
    :func:`modelcheck.__main__.main` catches :class:`cyclopts.CycloptsError`
    alongside the domain's own failures.
    """
    app = cyclopts.App(
        name="modelcheck",
        result_action="return_value",
        exit_on_error=False,
        help_on_error=False,
        print_error=False,
        config=_ENVIRONMENT,
    )

    @app.command
    def check(  # ruff: ignore[too-many-arguments] - one argument per option.
        model_dir: Path,
        *,
        fizz: Path,
        bounds: _BOUNDS = "routine",
        only: list[str] | None = None,
        json_path: typ.Annotated[Path | None, cyclopts.Parameter(name="--json")] = None,
        save_traces: bool = False,
        fixpoint: bool = False,
    ) -> int:
        """Run every configuration and report what each run proved.

        Parameters
        ----------
        model_dir : pathlib.Path
            The directory holding the model and its configurations.
        fizz : pathlib.Path
            The pinned checker to run.
        bounds : str
            Which declared bound set to run. The models declare only
            ``routine`` today, so ``nightly`` reaches the adapter and is
            refused there rather than being silently ignored.
        only : list[str] | None
            Report only these ``CONFIG[:PROPERTY]`` selections.
        json_path : pathlib.Path | None
            Write the matrix's judgements here as JSON.
        save_traces : bool
            Keep each counterexample's normalized trace beside the model.
        fixpoint : bool
            Compare every passing configuration's state count at its declared
            ``max_actions`` bound and at twice that bound.

        Returns
        -------
        int
            ``0`` when every expectation was met and, when asked for, every
            configuration's exploration was stable; ``1`` when any was not.
            ``--only`` narrows the reported expectations but never the status
            or the fixpoint check: both are about the model rather than a
            selection of it, so an omitted configuration can still fail the
            command.
        """
        source = source_factory(model_dir, bounds)
        results = service.run_matrix(source, checker_factory(fizz))
        chosen = select(results, only)
        print(report.render_report(chosen), end="")
        artefact = Artefact(json_path, chosen)
        artefact.write()
        traces = Traces(save_traces, model_dir)
        traces.save(chosen)
        status = exit_status(results)
        if fixpoint:
            return _compared(
                source, checker_factory(fizz), Outputs(artefact, traces), status
            )
        return status

    @app.command
    def drift(model_dir: Path) -> int:
        """Compare the model's mappings with the production surface.

        Parameters
        ----------
        model_dir : pathlib.Path
            The directory holding the model.

        Returns
        -------
        int
            ``0`` when the model accounts for every path it names, ``1`` when
            it does not.
        """
        found = _drift_report(source_factory(model_dir, "routine"), inventory_factory())
        print(report.render_drift(found), end="")
        return EXIT_MET if found.is_clean else EXIT_UNMET

    return app


def _compared(
    source: ConfigurationSource,
    checker: ModelChecker,
    outputs: Outputs,
    status: int,
) -> int:
    """Return the status once every passing configuration has been compared.

    Parameters
    ----------
    source : ConfigurationSource
        The model text and the configurations to compare.
    checker : ModelChecker
        The checker each comparison run is delegated to.
    outputs : Outputs
        The artefact the matrix already wrote, and where a comparison's
        counterexample path is kept.
    status : int
        The status the matrix alone produced.

    Returns
    -------
    int
        ``status`` when every comparison was stable, and ``1`` when one was
        not: an exploration that keeps growing is an unmet expectation even
        when every matrix row held.

    Raises
    ------
    FixpointError
        If a configuration did not pass at one of its two bounds. The artefact
        is rewritten with the failed comparison before the error leaves,
        because it was written before this check ran, and a failed
        comparison's counterexample is saved for the same reason: nothing
        downstream of the exception would otherwise see it.
    """
    try:
        stable = run_fixpoint(source, checker)
    except FixpointError as error:
        print(report.render_fixpoint(error.comparisons, error.failures), end="")
        outputs.artefact.write(error.failures, error.comparisons)
        outputs.traces.save_comparisons(error.failures)
        raise
    print(report.render_fixpoint(stable), end="")
    outputs.artefact.write(comparisons=stable)
    return status if all(found.is_stable for found in stable) else EXIT_UNMET


def _drift_report(
    source: ConfigurationSource, inventory: CodeInventory
) -> drift.DriftReport:
    """Build the drift report from a source and an inventory.

    Parameters
    ----------
    source : ConfigurationSource
        The model's text and its exclusions.
    inventory : CodeInventory
        The production surface and its resolution check.

    Returns
    -------
    modelcheck.drift.DriftReport
        The comparison.
    """
    return drift.drift(
        drift.parse_mappings(source.source_text()),
        inventory.items(),
        inventory.resolves,
        source.exclusions(),
    )
