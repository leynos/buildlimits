"""Assertions and readiness checks shared by host query scenarios."""

import dataclasses as dc
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed host probe argv.
import time
import typing as typ
from pathlib import Path

import pytest
from rustc_query_host_observations import result_evidence
from rustc_query_probe_records import (
    ProbeRecord,
    is_target_info_query,
    is_version_query,
    timing_entries,
)

if typ.TYPE_CHECKING:
    from rustc_query_host_support import ObservedProcess, ProcessResult
    from systemd_harness import Harness


@dc.dataclass(frozen=True, slots=True)
class QueueWait:
    """Deadline and identifying details for a linter compile queue wait."""

    observed: ObservedProcess
    timing_log: Path
    driver_name: str
    timeout: float


def require(*, condition: bool, message: str) -> None:
    """Raise a stable assertion when a host observation misses its contract.

    Raises
    ------
    AssertionError
        When the required host observation is false.
    """
    if not condition:
        raise AssertionError(message)


def tool_version(command: list[str], label: str) -> str:
    """Require a host tool's version command to succeed and return its output.

    Parameters
    ----------
    command : list[str]
        Executable and version-query arguments.
    label : str
        Tool label included in failure messages.

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
    require(
        condition=result.returncode == 0,
        message=f"{label} version failed: {result.stderr}",
    )
    return (result.stdout + result.stderr).strip()


def matching_dylint_driver(library: Path) -> tuple[Path, str, Path]:
    """Locate an installed driver and Rust toolchain matching the library name.

    Parameters
    ----------
    library : Path
        Prebuilt Dylint library whose toolchain determines the driver match.

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
            if _driver_matches_library(candidate, suffix)
        ),
        None,
    )
    if driver is None:
        pytest.skip(f"no installed Dylint driver matches library toolchain {suffix}")
    toolchain = _driver_toolchain(driver, _installed_toolchains())
    if toolchain is None:
        pytest.skip(f"Rust toolchain for Dylint driver {driver} is not installed")
    return driver, toolchain, driver_root


def _driver_matches_library(candidate: Path, suffix: str) -> bool:
    """Match installed driver directory names against the library suffix.

    Returns
    -------
    bool
        Whether the directory and library suffix identify the same toolchain.
    """
    directory = candidate.parent.name
    return (
        directory == suffix
        or directory.startswith(f"{suffix}-")
        or suffix.startswith(f"{directory}-")
    )


def _installed_toolchains() -> list[str]:
    """List rustup toolchains, retaining the existing readiness skip reasons.

    Returns
    -------
    list[str]
        Installed toolchain names.
    """
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
    return [line.split()[0] for line in result.stdout.splitlines() if line.split()]


def _driver_toolchain(driver: Path, toolchains: list[str]) -> str | None:
    """Find the installed toolchain whose name matches the driver directory.

    Returns
    -------
    str | None
        Matching toolchain name, if the driver directory is installed.
    """
    directory = driver.parent.name
    return next(
        (
            toolchain
            for toolchain in toolchains
            if toolchain == directory or toolchain.startswith(f"{directory}-")
        ),
        None,
    )


def wait_for_queued_compile(
    harness: Harness, wait: QueueWait
) -> tuple[dict[str, typ.Any], list[ProbeRecord]]:
    """Wait for resolution and exactly one queued real compile.

    Parameters
    ----------
    harness : Harness
        Running supervisor harness to inspect.
    wait : QueueWait
        Linter process, timing log and queue deadline.

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
            raise AssertionError(result_evidence(result, entries))
        time.sleep(0.05)
    msg = (
        f"{wait.driver_name} did not reach one queued compile: "
        f"status={latest}; probes={entries}; stderr={wait.observed.stderr_lines}; "
        f"stdout={wait.observed.stdout_path.read_text(errors='replace')}\n"
        + harness.journal()
    )
    raise AssertionError(msg)


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
        if Path(str(entry["argv"][0])).name == driver_name and is_query(entry)
    ]


def assert_driver_probes(entries: list[ProbeRecord], driver_name: str) -> None:
    """Require successful driver version and Cargo target-information probes.

    Parameters
    ----------
    entries : list[ProbeRecord]
        Completed probe records to inspect.
    driver_name : str
        Outer workspace-driver executable name.
    """
    driver_entries = _driver_probe_entries(entries, driver_name)
    require(
        condition=bool(driver_entries),
        message=f"no completed {driver_name} query: {entries}",
    )
    require(
        condition=all(entry["status"] == 0 for entry in driver_entries),
        message=str(driver_entries),
    )
    require(
        condition=any(is_version_query(entry) for entry in driver_entries),
        message=str(driver_entries),
    )
    require(
        condition=any(is_target_info_query(entry) for entry in driver_entries),
        message=str(driver_entries),
    )


def is_query(entry: ProbeRecord) -> bool:
    """Recognize version, print and exact Cargo target-information records.

    Parameters
    ----------
    entry : ProbeRecord
        Completed timing record to inspect.

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


def is_compile(entry: ProbeRecord) -> bool:
    """Recognize a completed compiler invocation that can generate artefacts.

    Parameters
    ----------
    entry : ProbeRecord
        Completed timing record to inspect.

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


def finish_or_fail(
    observed: ObservedProcess,
    timeout: float,
    harness: Harness,
    timing_log: Path,
) -> ProcessResult:
    """Add output and supervisor state if a host command exceeds its deadline.

    Parameters
    ----------
    observed : ObservedProcess
        Host command and its captured streams.
    timeout : float
        Maximum seconds to wait for completion.
    harness : Harness
        Supervisor harness included in timeout evidence.
    timing_log : Path
        Probe log included in timeout evidence.

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
        msg = (
            f"host command exceeded {timeout}s: {error}; "
            f"status={harness.status()}; probes={timing_entries(timing_log)}; "
            f"stderr={observed.stderr_lines}; "
            f"stdout={observed.stdout_path.read_text(errors='replace')}\n"
            + harness.journal()
        )
        raise AssertionError(msg) from error


def finish_barriers(processes: list[subprocess.Popen[bytes]]) -> None:
    """Reap every released barrier compiler and require it to exit normally.

    Parameters
    ----------
    processes : list[Popen[bytes]]
        Released barrier compiler processes to reap.
    """
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        require(
            condition=process.returncode == 0,
            message=f"barrier failed: {stdout!r} {stderr!r}",
        )
