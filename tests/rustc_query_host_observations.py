"""Format bounded host-command and probe evidence for acceptance reports."""

import datetime as dt
import json
import os
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from rustc_query_host_support import ProcessResult
    from rustc_query_probe_records import ProbeRecord


def result_evidence(result: ProcessResult, entries: list[ProbeRecord]) -> str:
    """Render bounded command and probe evidence for a failed assertion.

    Parameters
    ----------
    result : ProcessResult
        Completed command status, streams and timing.
    entries : list[ProbeRecord]
        Completed compiler probes associated with the command.

    Returns
    -------
    str
        Status, output and completed probe records.
    """
    return (
        f"status={result.status}; probes={entries}; stderr={result.stderr}; "
        f"stdout={result.stdout!r}"
    )


def probe_summary(entries: list[ProbeRecord]) -> list[dict[str, object]]:
    """Keep argv, status and duration for every completed timing record.

    Parameters
    ----------
    entries : list[ProbeRecord]
        Completed timing records to summarize.

    Returns
    -------
    list[dict[str, object]]
        Compact probe details suitable for JUnit properties.
    """
    summaries: list[dict[str, object]] = []
    for entry in entries:
        workspace_wrapper = entry["workspace_wrapper"]
        summaries.append({
            "argv": entry["argv"],
            "status": entry["status"],
            "duration_seconds": round(
                float(entry["ended"]) - float(entry["started"]), 6
            ),
            "workspace_wrapper": workspace_wrapper,
        })
    return summaries


def package_cache_waits(result: ProcessResult) -> list[dict[str, object]]:
    """Return package-cache lock waits with monotonic stderr arrival times.

    Parameters
    ----------
    result : ProcessResult
        Completed command containing captured stderr lines.

    Returns
    -------
    list[dict[str, object]]
        Timestamped lock-related Cargo stderr lines.
    """
    return [
        {"monotonic_at": timestamp, "line": line.rstrip()}
        for timestamp, line in result.stderr_lines
        if "lock" in line.casefold() and "cache" in line.casefold()
    ]


def record_observation(
    record_property: cabc.Callable[[str, object], None],
    prefix: str,
    result: ProcessResult,
    entries: list[ProbeRecord],
) -> None:
    """Attach host, UTC date, process timing and shim records to JUnit output.

    Parameters
    ----------
    record_property : Callable[[str, object], None]
        Pytest JUnit property recorder.
    prefix : str
        Prefix for each observation property key.
    result : ProcessResult
        Completed command status, output and timing.
    entries : list[ProbeRecord]
        Probe records collected during the command.
    """
    record_property(f"{prefix}_host", os.uname().nodename)
    record_property(f"{prefix}_date_utc", dt.datetime.now(dt.UTC).isoformat())
    record_property(f"{prefix}_status", str(result.status))
    record_property(f"{prefix}_started_monotonic", f"{result.started:.6f}")
    record_property(f"{prefix}_ended_monotonic", f"{result.ended:.6f}")
    record_property(
        f"{prefix}_duration_seconds", f"{result.ended - result.started:.6f}"
    )
    record_property(
        f"{prefix}_probes", json.dumps(probe_summary(entries), sort_keys=True)
    )
    record_property(
        f"{prefix}_package_cache_waits",
        json.dumps(package_cache_waits(result), sort_keys=True),
    )
