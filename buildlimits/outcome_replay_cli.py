"""The bounded, read-only cache-outcome replay command."""

import argparse
import json
import os
import sys
import typing as typ
from pathlib import Path

from buildlimits import outcome_history, outcome_records, outcome_replay
from buildlimits.outcome_history_args import add_history_arguments, selection_from
from buildlimits.outcome_store_io import read_store
from buildlimits.outcome_store_salt import store_path
from buildlimits.retry_notice import report_retry

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits import outcome_record_format


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
    add_history_arguments(parser)
    try:
        options = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    try:
        path = options.store if options.store is not None else store_path(os.environ)
    except OSError, ValueError:
        return _write_failure("unreadable", as_json=options.json)
    return _run(options, path)


def _run(options: argparse.Namespace, path: Path) -> int:
    """Read one history and print its report or its closed failure.

    Returns
    -------
    int
        Zero for a report, otherwise the bounded unreadable-store status.
    """
    selection = selection_from(options)
    try:
        if selection is None:
            decoded = read_store(path)
            if isinstance(decoded, outcome_records.RecordFailure):
                return _write_failure(decoded.reason, as_json=options.json)
            report = _evaluate(options.hypothesis, decoded.records)
        else:
            report = _history_report(path, selection, options.hypothesis)
    except FileNotFoundError:
        report = _empty_report(options.hypothesis)
    except outcome_history.HistoryFaultError as error:
        return _write_failure(error.reason, as_json=options.json)
    except OSError:
        return _write_failure("unreadable", as_json=options.json)
    _write_report(report, as_json=options.json)
    return 0


def _evaluate(
    hypothesis: str, records: cabc.Iterable[outcome_record_format.StoreRecord]
) -> outcome_replay.H2Report | outcome_replay.H3Report:
    """Evaluate one hypothesis over an iterable of rows.

    Returns
    -------
    H2Report | H3Report
        The selected hypothesis's report.
    """
    if hypothesis == "h2":
        return outcome_replay.evaluate_h2(records)
    return outcome_replay.evaluate_h3(records)


def _history_report(
    path: Path,
    selection: outcome_history.HistorySelection,
    hypothesis: str,
) -> outcome_replay.H2Report | outcome_replay.H3Report:
    """Stream retained history into one report, retrying a moved snapshot.

    A pass that finds the store changed after the stream pinned it is
    discarded whole and retried from a fresh snapshot, so H2's two passes
    always compare the same rows.

    Returns
    -------
    H2Report | H3Report
        The selected hypothesis's report.

    Raises
    ------
    HistoryFaultError
        If the store never settles, or a selected segment is corrupt.
    """
    last: outcome_history.HistoryFaultError | None = None
    for attempt in range(outcome_history.HISTORY_ATTEMPTS):
        stream = outcome_history.open_history_beside(
            path, selection, on_retry=report_retry
        )
        try:
            return _evaluate(hypothesis, stream)
        except outcome_history.HistoryFaultError as error:
            if error.reason != "unsettled":
                raise
            last = error
            report_retry("history", attempt + 1, "unsettled")
    if last is not None:
        raise last
    raise outcome_history.HistoryFaultError("unsettled")


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
    return outcome_replay.evaluate_h3(())


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
    else:
        print(
            "H3 parser prediction: "
            f"{report.verdict}; {report.attributed_outcomes} attributed outcomes, "
            f"{report.evaluated_windows} windows, "
            f"{report.counterexamples} counterexamples"
        )
    print(f"  covered {_covered_text(value)}")
    for caveat in typ.cast("list[str]", value["caveats"]):
        print(f"  caveat: {caveat}")


def _covered_text(value: dict[str, object]) -> str:
    """Describe the span of store history a verdict rests on.

    Returns
    -------
    str
        The covered ISO span, or a marker when the store held no records.
    """
    covered = typ.cast("dict[str, str | None]", value["covered"])
    if covered["from"] is None or covered["until"] is None:
        return "no records"
    return f"{covered['from']} to {covered['until']}"


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
