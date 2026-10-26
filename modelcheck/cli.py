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

import json
import typing as typ
from pathlib import Path

import cyclopts
import cyclopts.config

from modelcheck import domain, drift, report, service

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from modelcheck.domain import Judgement
    from modelcheck.ports import CodeInventory, ConfigurationSource

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


def make_app(  # pylint: disable=too-many-arguments  # ADR 004 supplies three adapters.
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
    ) -> int:
        """Run every configuration and report what each run proved.

        Parameters
        ----------
        model_dir : pathlib.Path
            The directory holding the model and its configurations.
        fizz : pathlib.Path
            The pinned checker to run.
        bounds : str
            Which declared bound set to run, ``routine`` or ``nightly``.
        only : list[str] | None
            Report only these ``CONFIG[:PROPERTY]`` selections.
        json_path : pathlib.Path | None
            Write the matrix's judgements here as JSON.
        save_traces : bool
            Keep each counterexample's normalized trace beside the model.

        Returns
        -------
        int
            ``0`` when every expectation was met, ``1`` when any was not.
        """
        source = source_factory(model_dir, bounds)
        chosen = _chosen(service.run_matrix(source, checker_factory(fizz)), only)
        print(report.render_report(chosen), end="")
        if json_path is not None:
            json_path.write_text(report.render_records(chosen), encoding="utf-8")
        if save_traces:
            _save_traces(chosen, model_dir)
        return exit_status(chosen)

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


def _chosen(
    judgements: cabc.Sequence[Judgement], only: list[str] | None
) -> tuple[Judgement, ...]:
    """Return the judgements a ``--only`` selection keeps.

    The matrix is always run in full. A selection narrows what is *reported*,
    not what is checked, so a run given ``--only`` cannot pass while a
    configuration it omitted would have failed: the exit status is computed
    from the same narrowed sequence as the report, and the caller who narrowed
    it has said which expectations this invocation is about.

    Parameters
    ----------
    judgements : collections.abc.Sequence[Judgement]
        Every judgement the matrix produced.
    only : list[str] | None
        The raw ``CONFIG[:PROPERTY]`` selections, or ``None`` for everything.

    Returns
    -------
    tuple[Judgement, ...]
        The selected judgements, in matrix order.
    """
    selections = _selections(only)
    return tuple(
        judgement for judgement in judgements if _selected(judgement, selections)
    )


def _selections(only: list[str] | None) -> tuple[tuple[str, str | None], ...]:
    """Split ``CONFIG[:PROPERTY]`` selections into their parts.

    A selection with no colon names every expectation of one configuration; one
    with a colon names a single property or probe of it.

    Parameters
    ----------
    only : list[str] | None
        The selections as the command line gave them.

    Returns
    -------
    tuple[tuple[str, str | None], ...]
        One pair per selection, the property being ``None`` when the whole
        configuration was named.

    Examples
    --------
    >>> _selections(["head", "incident-1:L3"])
    (('head', None), ('incident-1', 'L3'))
    >>> _selections(None)
    ()
    """
    return tuple(
        (configuration, subject or None)
        for selection in only or []
        for configuration, _, subject in (selection.partition(":"),)
    )


def _selected(
    judgement: Judgement, selections: tuple[tuple[str, str | None], ...]
) -> bool:
    """Report whether a judgement is one of the selected expectations.

    Parameters
    ----------
    judgement : Judgement
        The judgement to test.
    selections : tuple[tuple[str, str | None], ...]
        The parsed ``--only`` selections. Empty means everything is selected.

    Returns
    -------
    bool
        Whether the judgement's configuration is named, and its subject too
        when the selection named one.
    """
    if not selections:
        return True
    return any(
        configuration == judgement.configuration
        and (subject is None or subject == report.subject(judgement.expectation))
        for configuration, subject in selections
    )


def _save_traces(judgements: cabc.Sequence[Judgement], model_dir: Path) -> None:
    """Write each counterexample's normalized path beside the model.

    A trace is written only for a counterexample, because only a
    counterexample has one. The file name is the configuration and the subject,
    so a re-run replaces its own trace rather than accumulating a directory
    full of timestamps, and a review of the change sees the paths that moved.

    Parameters
    ----------
    judgements : collections.abc.Sequence[Judgement]
        The judgements whose counterexamples to keep.
    model_dir : pathlib.Path
        The model directory; traces go under its ``traces/``.
    """
    directory = model_dir / "traces"
    for judgement in judgements:
        if not isinstance(judgement.result, domain.Counterexample):
            continue
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{judgement.configuration}-{report.subject(judgement.expectation)}"
        record = report.trace_record(judgement.result.trace)
        (directory / f"{name}.json").write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8"
        )


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
