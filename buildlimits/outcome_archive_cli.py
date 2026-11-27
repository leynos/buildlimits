"""The timer-friendly command that maintains closed cache-outcome history.

``run`` archives sealed history and then applies the configured retention
policy. ``retain`` applies the same policy to the same history without
compressing anything, so a timer can enforce the bounds without paying for XZ.
``inspect`` reports the archive plan without changing a file. No command prints
a filesystem path or a raw exception, so a timer's journal stays free of
host-specific detail.
"""

import argparse
import os
import sys
import typing as typ
from pathlib import Path

from buildlimits import config
from buildlimits.journal_native import JournalSink
from buildlimits.metrics import Metrics
from buildlimits.outcome_archive import (
    RunOptions,
    RunReport,
    inspect_store,
    run_archive,
    run_retain,
)
from buildlimits.outcome_archive_exit import EXIT_USAGE, exit_status
from buildlimits.outcome_archive_render import write_report
from buildlimits.outcome_record_encode import header_floor_bytes
from buildlimits.outcome_record_format import MAX_COMPACT_BYTES
from buildlimits.outcome_retention import RetentionPolicy, policy_from_settings
from buildlimits.outcome_segments import SegmentLayout
from buildlimits.outcome_store_salt import store_path
from buildlimits.retry_notice import report_retry

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
        settings = config.load(config_path, header_floor=header_floor_bytes())
        layout = SegmentLayout.beside(_store(options.store, os.environ))
        policy = policy_from_settings(
            settings,
            header_floor=header_floor_bytes(),
            segment_limit=MAX_COMPACT_BYTES,
        )
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
    sink, metrics = _journal_metrics()
    selected = RunOptions(
        preset=settings.cache_outcomes_archive_preset,
        dry_run=getattr(options, "dry_run", False),
        on_retry=report_retry,
        metrics=metrics,
    )
    try:
        report = _report(options.command, layout, policy, selected)
    finally:
        if sink is not None:
            sink.close()
    write_report(report, as_json=options.json)
    return exit_status(report.status)


def _journal_metrics() -> tuple[JournalSink | None, Metrics]:
    """Return the journal publisher a timer firing uses, or a discarding one.

    A command run outside a service has no journal stream to address, so it
    keeps the null sink rather than counting every record as undelivered. The
    sink is returned beside the port so the caller can close it after the one
    record this firing publishes.

    Returns
    -------
    tuple[JournalSink | None, Metrics]
        The sink to close, or None, and the validated event port.
    """
    if "JOURNAL_STREAM" not in os.environ:
        return None, Metrics(source="adapter")
    sink = JournalSink(identifier="build-limits-outcome-archive")
    return sink, Metrics(sink, source="adapter")


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
    retain = subcommands.add_parser(
        "retain",
        parents=[common],
        help="apply the retention policy without archiving closed history",
    )
    retain.add_argument(
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
    command: str,
    layout: SegmentLayout,
    policy: RetentionPolicy,
    selected: RunOptions,
) -> RunReport:
    """Run the selected subcommand and return its report.

    Returns
    -------
    RunReport
        The command's bounded result.
    """
    if command == "inspect":
        return inspect_store(layout, policy, options=selected)
    if command == "retain":
        return run_retain(layout, policy, selected)
    return run_archive(layout, policy, selected)


if __name__ == "__main__":
    raise SystemExit(main())
