"""The bounded, read-only cache-outcome replay command."""

import argparse
import json
import os
import sys
from pathlib import Path

from buildlimits import outcome_records, outcome_replay
from buildlimits.outcome_store_io import read_store
from buildlimits.outcome_store_salt import store_path


def main(argv: list[str] | None = None) -> int:
    """Run one read-only hypothesis replay over the configured store.

    Returns
    -------
    int
        Zero for a report, one for unreadable or invalid data, and two for
        invalid command-line options.
    """
    parser = argparse.ArgumentParser(prog="build-limits-cache-replay")
    parser.add_argument("hypothesis", choices=("h2", "h3"))
    parser.add_argument("--store", type=Path, help="read this store instead")
    parser.add_argument("--json", action="store_true", help="print JSON output")
    try:
        options = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    try:
        path = options.store if options.store is not None else store_path(os.environ)
    except OSError, ValueError:
        return _write_failure("unreadable", as_json=options.json)
    try:
        decoded = read_store(path)
    except FileNotFoundError:
        report = _empty_report(options.hypothesis)
        _write_report(report, as_json=options.json)
        return 0
    except OSError:
        return _write_failure("unreadable", as_json=options.json)
    if isinstance(decoded, outcome_records.RecordFailure):
        return _write_failure(decoded.reason, as_json=options.json)
    report = (
        outcome_replay.evaluate_h2(decoded.records)
        if options.hypothesis == "h2"
        else outcome_replay.evaluate_h3(decoded.records)
    )
    _write_report(report, as_json=options.json)
    return 0


def _empty_report(
    hypothesis: str,
) -> outcome_replay.H2Report | outcome_replay.H3Report:
    """Return the selected hypothesis's empty-store result.

    Returns
    -------
    H2Report | H3Report
        The selected hypothesis's zero-evidence report.
    """
    if hypothesis == "h2":
        return outcome_replay.evaluate_h2(())
    return outcome_replay.H3Report("insufficient", 0, 0, 0, 0)


def _write_report(
    report: outcome_replay.H2Report | outcome_replay.H3Report, *, as_json: bool
) -> None:
    """Render one stable report without exposing the store path."""
    value = report.as_dict()
    if as_json:
        print(json.dumps(value, sort_keys=True))
        return
    if isinstance(report, outcome_replay.H2Report):
        print(
            "H2 child/runtime prediction: "
            f"{report.verdict}; {report.evaluable_records} evaluable of "
            f"{report.attributed_outcomes} attributed outcomes; "
            f"combined {_format_agreement(report.combined_agreement)}; "
            f"runtime {_format_agreement(report.runtime_only_agreement)}; "
            f"child-only {_format_agreement(report.child_only_agreement)}; "
            f"misses seen {report.misses_seen}/{report.misses_with_child_observation}"
        )
        return
    print(
        "H3 parser prediction: "
        f"{report.verdict}; {report.attributed_outcomes} attributed outcomes, "
        f"{report.evaluated_windows} windows, {report.counterexamples} counterexamples"
    )


def _format_agreement(value: float | None) -> str:
    """Render one agreement fraction without inventing a zero-rate result.

    Returns
    -------
    str
        A percentage or the bounded marker for no observations.
    """
    return "n/a" if value is None else f"{value:.1%}"


def _write_failure(reason: str, *, as_json: bool) -> int:
    """Print a closed store failure without a path or raw exception.

    Returns
    -------
    int
        The command's bounded unreadable-store exit status.
    """
    if as_json:
        print(json.dumps({"schema": outcome_replay.REPLAY_SCHEMA, "reason": reason}))
    else:
        print(f"build-limits-cache-replay: {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
