"""Assertions and readiness checks shared by host query scenarios."""

import dataclasses as dc
import datetime as dt
import json
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed host probe argv.
import time
import typing as typ
from pathlib import Path

import pytest
from rustc_query_host_support import (
    ObservedProcess,
    ProbeRecord,
    ProcessResult,
    is_target_info_query,
    is_version_query,
    timing_entries,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from systemd_harness import Harness


@dc.dataclass(frozen=True, slots=True)
class QueueWait:
    """Deadline and identifying details for a linter compile queue wait."""

    observed: ObservedProcess
    timing_log: Path
    driver_name: str
    timeout: float


def _require(*, condition: bool, message: str) -> None:
    """Raise a stable assertion when a host observation misses its contract.

    Raises
    ------
    AssertionError
        When the required host observation is false.
    """
    if not condition:
        raise AssertionError(message)


def _version(command: list[str], label: str) -> str:
    """Require a host tool's version command to succeed and return its output.

    Returns
    -------
    str
        Version output from standard output and standard error.
    """
    executable = shutil.which(command[0])
    if executable is None:
        pytest.skip(f"{command[0]} is unavailable")
    environment = os.environ.copy()
    for name in ("RUSTC", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER"):
        environment.pop(name, None)
    environment.pop("RUSTUP_TOOLCHAIN", None)
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed host probe argv.
        [executable, *command[1:]],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        timeout=60,
    )
    _require(
        condition=result.returncode == 0,
        message=f"{label} version failed: {result.stderr}",
    )
    return (result.stdout + result.stderr).strip()


def _matching_dylint_driver(library: Path) -> tuple[Path, str, Path]:
    """Locate an installed driver and Rust toolchain matching the library name.

    Returns
    -------
    tuple[Path, str, Path]
        The driver, its Rust toolchain name, and the driver root.
    """
    suffix = library.name.split("@", 1)[1].split(".", 1)[0]
    driver_root = Path(
        os.environ.get("DYLINT_DRIVER_PATH", Path.home() / ".dylint_drivers")
    )
    matches = sorted(driver_root.glob("*/dylint-driver"))
    driver = next(
        (
            candidate
            for candidate in matches
            if candidate.parent.name == suffix
            or candidate.parent.name.startswith(f"{suffix}-")
            or suffix.startswith(f"{candidate.parent.name}-")
        ),
        None,
    )
    if driver is None:
        pytest.skip(f"no installed Dylint driver matches library toolchain {suffix}")
    rustup = shutil.which("rustup")
    if rustup is None:
        pytest.skip("rustup is unavailable for the Dylint driver")
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed rustup query.
        [rustup, "toolchain", "list", "--quiet"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.skip("rustup toolchain list is unavailable for the Dylint driver")
    toolchain = next(
        (
            line.split()[0]
            for line in result.stdout.splitlines()
            if line.split()
            and (
                line.split()[0] == driver.parent.name
                or line.split()[0].startswith(f"{driver.parent.name}-")
            )
        ),
        None,
    )
    if toolchain is None:
        pytest.skip(f"Rust toolchain for Dylint driver {driver} is not installed")
    return driver, toolchain, driver_root


def _wait_for_queued_compile(
    harness: Harness, wait: QueueWait
) -> tuple[dict[str, typ.Any], list[ProbeRecord]]:
    """Wait for resolution and exactly one queued real compile.

    Returns
    -------
    tuple[dict[str, Any], list[ProbeRecord]]
        Current pool status and all completed timing records.

    Raises
    ------
    AssertionError
        If the linter exits early or misses the queue deadline.
    """
    deadline = time.monotonic() + wait.timeout
    latest: dict[str, typ.Any] = {}
    entries: list[ProbeRecord] = []
    while time.monotonic() < deadline:
        latest = harness.status()
        entries = timing_entries(wait.timing_log)
        if len(latest["queued"]) == 1 and _driver_probe_entries(
            entries, wait.driver_name
        ):
            return latest, entries
        if wait.observed.process.poll() is not None:
            result = wait.observed.finish(timeout=0)
            raise AssertionError(_result_evidence(result, entries))
        time.sleep(0.05)
    raise AssertionError(
        f"{wait.driver_name} did not reach one queued compile: "
        f"status={latest}; probes={entries}; stderr={wait.observed.stderr_lines}; "
        f"stdout={wait.observed.stdout_path.read_text(errors='replace')}\n"
        + harness.journal()
    )


def _driver_probe_entries(
    entries: list[ProbeRecord], driver_name: str
) -> list[ProbeRecord]:
    """Select successful query records whose outer program is the named driver.

    Returns
    -------
    list[ProbeRecord]
        Version, print, or Cargo query records for the selected driver.
    """
    return [
        entry
        for entry in entries
        if Path(str(entry["argv"][0])).name == driver_name and _is_query(entry)
    ]


def _assert_driver_probes(entries: list[ProbeRecord], driver_name: str) -> None:
    """Require successful driver version and Cargo target-information probes."""
    driver_entries = _driver_probe_entries(entries, driver_name)
    _require(
        condition=bool(driver_entries),
        message=f"no completed {driver_name} query: {entries}",
    )
    _require(
        condition=all(entry["status"] == 0 for entry in driver_entries),
        message=str(driver_entries),
    )
    _require(
        condition=any(is_version_query(entry) for entry in driver_entries),
        message=str(driver_entries),
    )
    _require(
        condition=any(is_target_info_query(entry) for entry in driver_entries),
        message=str(driver_entries),
    )


def _is_query(entry: ProbeRecord) -> bool:
    """Recognize version, print and exact Cargo target-information records.

    Returns
    -------
    bool
        Whether the timing record describes a compiler query.
    """
    argv = entry["argv"]
    return (
        is_version_query(entry)
        or is_target_info_query(entry)
        or any(
            argument == "--print" or argument.startswith("--print=")
            for argument in argv
        )
    )


def _is_compile(entry: ProbeRecord) -> bool:
    """Recognize a completed compiler invocation that can generate artefacts.

    Returns
    -------
    bool
        Whether the invocation requests an output artefact.
    """
    argv = entry["argv"]
    return any(
        argument == "--emit" or argument.startswith("--emit=") or argument == "-o"
        for argument in argv
    )


def _finish_or_fail(
    observed: ObservedProcess,
    timeout: float,
    harness: Harness,
    timing_log: Path,
) -> ProcessResult:
    """Add output and supervisor state if a host command exceeds its deadline.

    Returns
    -------
    ProcessResult
        The completed command result.

    Raises
    ------
    AssertionError
        If the command misses its deadline; the message includes host evidence.
    """
    try:
        return observed.finish(timeout)
    except subprocess.TimeoutExpired as error:
        raise AssertionError(
            f"host command exceeded {timeout}s: {error}; "
            f"status={harness.status()}; probes={timing_entries(timing_log)}; "
            f"stderr={observed.stderr_lines}; "
            f"stdout={observed.stdout_path.read_text(errors='replace')}\n"
            + harness.journal()
        ) from error


def _finish_barriers(processes: list[subprocess.Popen[bytes]]) -> None:
    """Reap every released barrier compiler and require it to exit normally."""
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        _require(
            condition=process.returncode == 0,
            message=f"barrier failed: {stdout!r} {stderr!r}",
        )


def _result_evidence(result: ProcessResult, entries: list[ProbeRecord]) -> str:
    """Render bounded command and probe evidence for a failed assertion.

    Returns
    -------
    str
        Status, output and completed probe records.
    """
    return (
        f"status={result.status}; probes={entries}; stderr={result.stderr}; "
        f"stdout={result.stdout!r}"
    )


def _probe_summary(entries: list[ProbeRecord]) -> list[dict[str, object]]:
    """Keep argv, status and duration for every completed timing record.

    Returns
    -------
    list[dict[str, object]]
        Compact probe details suitable for JUnit properties.
    """
    return [
        {
            "argv": entry["argv"],
            "status": entry["status"],
            "duration_seconds": round(
                float(entry["ended"]) - float(entry["started"]), 6
            ),
            "workspace_wrapper": typ.cast("dict[str, object]", entry).get(
                "workspace_wrapper"
            ),
        }
        for entry in entries
    ]


def _package_cache_waits(result: ProcessResult) -> list[dict[str, object]]:
    """Return package-cache lock waits with monotonic stderr arrival times.

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


def _record_observation(
    record_property: cabc.Callable[[str, object], None],
    prefix: str,
    result: ProcessResult,
    entries: list[ProbeRecord],
) -> None:
    """Attach host, UTC date, process timing and shim records to JUnit output."""
    record_property(f"{prefix}_host", os.uname().nodename)
    record_property(f"{prefix}_date_utc", dt.datetime.now(dt.UTC).isoformat())
    record_property(f"{prefix}_status", str(result.status))
    record_property(f"{prefix}_started_monotonic", f"{result.started:.6f}")
    record_property(f"{prefix}_ended_monotonic", f"{result.ended:.6f}")
    record_property(
        f"{prefix}_duration_seconds", f"{result.ended - result.started:.6f}"
    )
    record_property(
        f"{prefix}_probes", json.dumps(_probe_summary(entries), sort_keys=True)
    )
    record_property(
        f"{prefix}_package_cache_waits",
        json.dumps(_package_cache_waits(result), sort_keys=True),
    )
