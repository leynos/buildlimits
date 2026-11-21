"""The timer-friendly command that archives closed cache-outcome history.

``run`` applies the configured retention policy and exits with a status a
systemd timer can act on. ``inspect`` reports the same plan without changing a
file. Neither command prints a filesystem path or a raw exception, so a timer's
journal stays free of host-specific detail.
"""

import argparse
import json
import os
import sys
import typing as typ
from pathlib import Path

from buildlimits import config
from buildlimits.outcome_archive import (
    RunOptions,
    RunReport,
    inspect_store,
    run_archive,
)
from buildlimits.outcome_archive_codec import report_dict
from buildlimits.outcome_archive_exit import EXIT_USAGE, exit_status
from buildlimits.outcome_record_encode import header_floor_bytes
from buildlimits.outcome_retention import RetentionPolicy, policy_from_settings
from buildlimits.outcome_segments import SegmentLayout
from buildlimits.outcome_store_salt import store_path
from buildlimits.utc_text import utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Run one archive command against the configured outcome store.

    Parameters
    ----------
    argv : Sequence[str] | None
        Command-line arguments; ``sys.argv[1:]`` when ``None``.

    Returns
    -------
    int
        The report's bounded exit status, or two for a usage or config error.

    Examples
    --------
    ``build-limits-outcome-archive inspect`` reports the current plan and
    changes nothing; ``run`` performs it.
    """
    parser = _build_parser()
    try:
        options = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else EXIT_USAGE
    config_path = _default_config() if options.config is None else options.config
    try:
        settings = config.load(config_path)
        layout = SegmentLayout.beside(_store(options.store, os.environ))
        policy = policy_from_settings(settings, header_floor=header_floor_bytes())
    except config.ConfigError as error:
        # An unreadable configuration is the one documented path exception: the
        # operator must learn which file failed before any report exists.
        print(f"build-limits-outcome-archive: {error}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as error:
        # ``strerror`` never carries the file name, so a store fault stays
        # free of host-specific detail in a timer's journal.
        print(
            f"build-limits-outcome-archive: {error.strerror or 'store is unreadable'}",
            file=sys.stderr,
        )
        return EXIT_USAGE
    except ValueError as error:
        # Store and policy failures carry a closed message this package
        # authored, which names the missing setting rather than a path.
        print(f"build-limits-outcome-archive: {error}", file=sys.stderr)
        return EXIT_USAGE
    report = _report(options, layout, settings, policy)
    _write_report(report, as_json=options.json)
    return exit_status(report.status)


def _default_config() -> Path:
    """Return the per-user supervisor configuration read when none is named.

    The home directory is resolved when the command runs rather than when this
    module is imported, so a caller's environment decides which account's
    configuration is read.

    Returns
    -------
    Path
        ``~/.config/buildlimits/supervisor.toml`` under the current home.
    """
    return Path.home() / ".config" / "buildlimits" / "supervisor.toml"


def _build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser and its two subcommands.

    Returns
    -------
    argparse.ArgumentParser
        A parser whose subcommand is required.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--store", type=Path, help="the cache-outcomes JSON Lines path")
    common.add_argument(
        "--config",
        type=Path,
        help="the supervisor configuration file "
        "(default: ~/.config/buildlimits/supervisor.toml)",
    )
    common.add_argument("--json", action="store_true", help="print the report as JSON")
    parser = argparse.ArgumentParser(
        prog="build-limits-outcome-archive",
        description="Archive closed cache-outcome history and apply retention.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser(
        "run",
        parents=[common],
        help="archive closed history and apply the retention policy",
    )
    run.add_argument(
        "--dry-run", action="store_true", help="plan without changing any file"
    )
    subcommands.add_parser(
        "inspect",
        parents=[common],
        help="report retained history and the plan a run would execute",
    )
    return parser


def _store(explicit: Path | None, environ: cabc.Mapping[str, str]) -> Path:
    """Resolve the active store path from the option or the environment.

    Parameters
    ----------
    explicit : Path | None
        The ``--store`` value, or None to derive the path from ``environ``.
    environ : Mapping[str, str]
        The process environment the caller supplied, read only when no store
        is named.

    Returns
    -------
    Path
        The configured ``cache-outcomes.jsonl`` path as an absolute path, so a
        relative ``--store`` resolves against the process working directory
        exactly as the layout beside it does. A ``ValueError`` from
        ``store_path`` propagates when the environment names no usable root.
    """
    if explicit is not None:
        return explicit.absolute()
    return store_path(environ)


def _report(
    options: argparse.Namespace,
    layout: SegmentLayout,
    settings: config.SupervisorConfig,
    policy: RetentionPolicy,
) -> RunReport:
    """Run the selected subcommand and return its report.

    Returns
    -------
    RunReport
        The command's bounded result.
    """
    if options.command == "inspect":
        return inspect_store(layout, policy)
    return run_archive(
        layout,
        policy,
        RunOptions(
            preset=settings.cache_outcomes_archive_preset,
            dry_run=options.dry_run,
        ),
    )


def _write_report(report: RunReport, *, as_json: bool) -> None:
    """Render one report as JSON or as bounded operator lines.

    Parameters
    ----------
    report : RunReport
        The run or inspection result.
    as_json : bool
        Whether to print the stable JSON document instead of prose.
    """
    if as_json:
        print(json.dumps(report_dict(report), sort_keys=True))
        return
    print(f"status: {report.status}")
    if report.reason is not None:
        print(f"reason: {report.reason}")
    print(f"retained: {report.retained_bytes} of {report.budget_bytes} logical bytes")
    _write_history(report)
    print(f"active: {report.active_bytes} logical bytes")
    print(f"cutoff: {_cutoff_text(report)}")
    _write_plan(report)
    _write_actions(report)
    _write_diagnostics(report)


def _write_history(report: RunReport) -> None:
    """Print the retained rows and the span their observations cover."""
    span = "unknown"
    if report.observed_from is not None and report.observed_until is not None:
        span = f"{utc_text(report.observed_from)}..{utc_text(report.observed_until)}"
    unmeasured = (
        ""
        if report.unmeasured_entries == 0
        else f", {report.unmeasured_entries} unmeasured"
    )
    print(f"history: {report.retained_records} records{unmeasured}, observed {span}")


def _cutoff_text(report: RunReport) -> str:
    """Describe the age cutoff a timer should display.

    Returns
    -------
    str
        The UTC cutoff, or a marker when age eviction is disabled.
    """
    if report.cutoff is None:
        return "age eviction disabled"
    return report.cutoff.isoformat()


def _write_plan(report: RunReport) -> None:
    """Print the plan's counts when the report carries one."""
    plan = report.plan
    if plan is None:
        return
    print(
        "plan: "
        f"archive {plan.archive}, reconcile {plan.reconcile}, heal {plan.heal}, "
        f"orphan {plan.orphan}, trim {plan.trim}, delete {plan.delete}"
    )
    if plan.retention_deferred:
        # The plan's trim and delete counts are provisional until a settled
        # pass has measured the store a budget applies to.
        print("retention: deferred; trims and deletes are decided once settled")
    if report.dry_run:
        print("dry run: no file was changed")


def _write_actions(report: RunReport) -> None:
    """Print one line per action name with how often it occurred."""
    counts: dict[str, int] = {}
    for action in report.actions:
        counts[action.action] = counts.get(action.action, 0) + 1
    for name in sorted(counts):
        print(f"action {name}: {counts[name]}")


def _write_diagnostics(report: RunReport) -> None:
    """Print the integrity flags an operator may need to act on."""
    _print_sequences("gaps", report.gaps)
    if report.gaps_truncated:
        print(f"gaps truncated: more than {len(report.gaps)} sequences are missing")
    _print_sequences("missing sidecar", report.missing_sidecars)
    _print_sequences("corrupt sidecar", report.corrupt_sidecars)
    _print_sequences("orphan sidecar", report.orphan_sidecars)
    for sequence, reason in report.failures:
        print(f"failure: sequence {sequence}: {reason}")


def _print_sequences(label: str, sequences: tuple[int, ...]) -> None:
    """Print one comma-separated integrity line when it is not empty.

    Parameters
    ----------
    label : str
        The closed diagnostic name.
    sequences : tuple[int, ...]
        The sequences the diagnostic names.
    """
    if sequences:
        joined = ", ".join(str(sequence) for sequence in sequences)
        print(f"{label}: {joined}")


if __name__ == "__main__":
    raise SystemExit(main())
