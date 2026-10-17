"""The bounded development-host journal consumer, build-limits-metrics."""

import argparse
import json
import sys
import typing as typ

from buildlimits import metrics_query
from buildlimits.metrics_report import report

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def main(
    argv: cabc.Sequence[str] | None = None,
    query: cabc.Callable[..., metrics_query.Window] = metrics_query.query,
) -> int:
    """Print journal aggregates, returning explicit query and usage statuses.

    Parameters
    ----------
    argv : Sequence[str] | None
        Command-line arguments, or process arguments when absent.
    query : Callable[..., Window]
        Injected journal-query transport.

    Returns
    -------
    int
        0 for a report, 1 for query failure, 2 for invalid usage.
    """
    parser = argparse.ArgumentParser(prog="build-limits-metrics")
    parser.add_argument(
        "--since", default="1h", help="positive lookback, e.g. 30m or 1h"
    )
    parser.add_argument(
        "--max-records", type=int, default=metrics_query.DEFAULT_MAX_RECORDS
    )
    parser.add_argument(
        "--source", choices=("all", "supervisor", "adapter"), default="all"
    )
    parser.add_argument(
        "--json", action="store_true", help="include bounded event history as JSON"
    )
    try:
        options = parser.parse_args(argv)
        metrics_query.window_seconds(options.since)
        if not 1 <= options.max_records <= metrics_query.MAX_RECORDS:
            parser.error("--max-records must be between 1 and 100000")
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    except ValueError as error:
        print(f"build-limits-metrics: {error}", file=sys.stderr)
        return 2
    try:
        window = query(
            since=options.since, max_records=options.max_records, source=options.source
        )
    except metrics_query.MetricsQueryError as error:
        print(f"build-limits-metrics: {error}", file=sys.stderr)
        return 1
    result = report(window)
    if not window.records:
        print(
            "build-limits-metrics: no accepted records in this window", file=sys.stderr
        )
    if window.truncated:
        print(
            "build-limits-metrics: record cap reached; history may be truncated",
            file=sys.stderr,
        )
    print(
        json.dumps(result, indent=2, sort_keys=True)
        if options.json
        else _render(result)
    )
    return 0


def _render(result: dict[str, object]) -> str:
    """Render all aggregates compactly, leaving detailed history to JSON.

    Returns
    -------
    str
        A readable report with explicit trust and truncation status.
    """
    header = [
        "Compile-admission metrics (journal observations)",
        "Supervisor: trusted user unit; adapters: self-reported",
    ]
    return "\n".join(
        header
        + [
            f"{name}: {json.dumps(value, sort_keys=True)}"
            for name, value in result.items()
            if name != "history"
        ]
    )
