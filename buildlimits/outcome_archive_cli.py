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
    EXIT_USAGE,
    RunOptions,
    RunReport,
    inspect_store,
    run_archive,
)
from buildlimits.outcome_archive_report import eviction_counts
from buildlimits.outcome_retention import RetentionPolicy, policy_from_settings
from buildlimits.outcome_segments import SegmentLayout
from buildlimits.outcome_store_salt import store_path

_DEFAULT_CONFIG = Path.home() / ".config" / "buildlimits" / "supervisor.toml"

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
    try:
        settings = config.load(options.config)
        layout = SegmentLayout.beside(_store(options.store))
        policy = policy_from_settings(settings)
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
    return report.exit_status


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
        default=_DEFAULT_CONFIG,
        help="the supervisor configuration file",
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


def _store(explicit: Path | None) -> Path:
    """Resolve the active store path from the option or the environment.

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
    return store_path(os.environ)


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
        print(json.dumps(report.as_dict(), sort_keys=True))
        return
    print(f"status: {report.status}")
    if report.reason is not None:
        print(f"reason: {report.reason}")
    print(f"retained: {report.retained_bytes} of {report.budget_bytes} logical bytes")
    print(f"active: {report.active_bytes} logical bytes")
    print(f"cutoff: {_cutoff_text(report)}")
    _write_limits(report)
    _write_history(report)
    _write_plan(report)
    _write_actions(report)
    _write_evictions(report)
    _write_diagnostics(report)
    _write_maintenance(report)


def _write_limits(report: RunReport) -> None:
    """Print the effective policy in the same units the JSON report uses."""
    limits = report.limits
    if limits is None:
        return
    age = f"{limits.max_age_days} days" if limits.age_eviction else "disabled"
    logical = str(limits.max_bytes) if limits.byte_eviction else "unlimited"
    records = "none" if limits.max_records is None else str(limits.max_records)
    mode = "segmented" if limits.segmented else "in-place"
    print(
        f"limits: age {age}, {logical} logical bytes, {records} record cap, "
        f"{limits.headroom_bytes} headroom bytes, {mode}"
    )


def _write_history(report: RunReport) -> None:
    """Print the retained row count, bytes and observation span."""
    retained = report.retained
    if retained is None:
        return
    span = _span_text(retained.span_seconds)
    print(
        f"history: {retained.record_count} records, "
        f"{retained.logical_bytes} logical bytes, span {span}"
    )
    if not retained.complete:
        print("history: retained count is a lower bound; some rows lack metadata")


def _span_text(seconds: int | None) -> str:
    """Describe a retained observation span.

    Returns
    -------
    str
        Whole seconds, or a marker when no row time was counted.
    """
    return "unknown" if seconds is None else f"{seconds} seconds"


def _write_evictions(report: RunReport) -> None:
    """Print how many trims and deletions each bound caused."""
    counts = eviction_counts(report.actions)
    if not any(counts.values()):
        return
    print(
        f"evictions: age {counts['age']}, bytes {counts['bytes']}, "
        f"age and bytes {counts['age_and_bytes']}"
    )


def _write_maintenance(report: RunReport) -> None:
    """Print a state-persistence failure an operator may need to act on."""
    if not report.maintenance.state_written:
        print(f"state: not written ({report.maintenance.reason})")


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
