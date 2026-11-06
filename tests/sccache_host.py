"""Support the opt-in real-sccache acceptance checks."""

import json
import os
import shutil
import socket
import struct

# ruff: ignore[suspicious-subprocess-import] - fixed argv, no shell.
import subprocess
import time
import typing as typ
from contextlib import contextmanager

import pytest
from host_check_marks import (
    HostCheck,
    decoded_document,
    end_process,
    host_check_marks,
)
from sccache_stats import _stats_payload

from buildlimits.outcome_recorder import SUPPORTED_SCCACHE_VERSIONS

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

_WINDOW_COUNT = 200
_STOP_TIMEOUT_SECONDS = 5.0

_LINUX_REASON = "the host check requires Linux /proc and SO_PEERCRED"
_OPT_IN_REASON = "set BUILD_LIMITS_SCCACHE_HOST_CHECK=1 to run the host check"
_VERSION_ERROR_REASON = "could not verify the sccache version"
_UNSUPPORTED_VERSION_REASON = "the host check requires a supported sccache version"


def _read_stats(
    executable: str, environment: cabc.Mapping[str, str]
) -> dict[str, object]:
    """Read one JSON snapshot without asking sccache to start a server.

    Returns
    -------
    dict[str, object]
        The decoded sccache statistics document.

    Raises
    ------
    ValueError
        If the statistics document reports an unsupported sccache version.
    """
    # ruff: ignore[subprocess-without-shell-equals-true] - supplied argv, no shell.
    completed = subprocess.run(
        [executable, "--show-stats", "--stats-format", "json"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
        timeout=15,
    )
    document = decoded_document(completed.stdout, source="sccache")
    if document.get("version") not in SUPPORTED_SCCACHE_VERSIONS:
        message = "sccache returned statistics for an unsupported version"
        raise ValueError(message)
    _stats_payload(document)
    return document


def _base_environment(tmp_path: Path, toolchain_bin: Path) -> dict[str, str]:
    """Build a private environment with the fake compiler first on PATH.

    Returns
    -------
    dict[str, str]
        The isolated sccache and fake-compiler environment.
    """
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("SCCACHE_")
        and key
        not in {
            "RUSTC",
            "RUSTC_WRAPPER",
            "RUSTC_WORKSPACE_WRAPPER",
        }
    }
    home = tmp_path / "home"
    home.mkdir()
    sysroot = tmp_path / "fake-rustc-sysroot"
    (sysroot / "lib").mkdir(parents=True)
    environment["HOME"] = str(home)
    environment["XDG_CONFIG_HOME"] = str(home / ".config")
    environment["FAKE_RUSTC_SYSROOT"] = str(sysroot)
    environment["PATH"] = os.pathsep.join((
        str(toolchain_bin),
        environment.get("PATH", "/usr/bin:/bin"),
    ))
    environment["SCCACHE_DIR"] = str(tmp_path / "cache")
    environment["SCCACHE_SERVER_UDS"] = str(tmp_path / "s.sock")
    environment["SCCACHE_IDLE_TIMEOUT"] = "0"
    environment["SCCACHE_NO_DAEMON"] = "1"
    environment["FAKE_RUSTC_EMIT_SECONDS"] = "1.0"
    environment["FAKE_RUSTC_SLEEP_EMIT_METADATA"] = "a9-cold"
    environment["FAKE_RUSTC_DEP_INFO_SECONDS"] = "0.2"
    environment["FAKE_RUSTC_SLEEP_DEP_INFO_METADATA"] = "a9-cold"
    return environment


def _systemd_scope_prefix(environment: cabc.Mapping[str, str]) -> list[str] | None:
    """Find a tested user-scope command prefix when a manager is available.

    Returns
    -------
    list[str] | None
        The working systemd-run prefix, or ``None`` when unavailable.
    """
    systemd_run = shutil.which("systemd-run")
    true_program = shutil.which("true")
    if systemd_run is None or true_program is None:
        return None

    prefix = [systemd_run, "--user", "--scope", "--wait", "--quiet"]
    # ruff: ignore[subprocess-without-shell-equals-true] - probe argv, no shell.
    probe = subprocess.run(
        [*prefix, true_program],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=15,
    )
    return prefix if probe.returncode == 0 else None


@contextmanager
def _private_server(
    executable: str,
    environment: dict[str, str],
    log_path: Path,
) -> cabc.Generator[tuple[subprocess.Popen[bytes], bool]]:
    """Run a private foreground server and stop it even when an assertion fails.

    Example
    -------
    ``with _private_server(sccache, env, log) as (process, scoped): ...``

    Yields
    ------
    tuple[subprocess.Popen[bytes], bool]
        The server launcher and whether it runs in a systemd scope.

    """
    prefix = _systemd_scope_prefix(environment)
    server_environment = {**environment, "SCCACHE_START_SERVER": "1"}
    command = [executable]
    if prefix is not None:
        command = [*prefix, *command]

    with log_path.open("wb") as log_file:
        # ruff: ignore[subprocess-without-shell-equals-true] - server argv, no shell.
        process = subprocess.Popen(
            command,
            env=server_environment,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        try:
            _await_ready(executable, environment, process, log_path)
            yield process, prefix is not None
        finally:
            _stop_server(executable, environment, process)


def _await_ready(
    executable: str,
    environment: dict[str, str],
    process: subprocess.Popen[bytes],
    log_path: Path,
) -> None:
    """Block until the private server answers statistics and reports its pid."""
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"sccache server exited early; see {log_path}")
        try:
            _read_stats(executable, environment)
            _server_pid(environment)
        except OSError, subprocess.CalledProcessError, json.JSONDecodeError:
            time.sleep(0.05)
        else:
            return
    pytest.fail(f"sccache server did not become ready; see {log_path}")


def _stop_server(
    executable: str, environment: dict[str, str], process: subprocess.Popen[bytes]
) -> None:
    """Stop the private server, bounding every wait for it to exit."""
    # ruff: ignore[subprocess-without-shell-equals-true] - stop argv, no shell.
    subprocess.run(
        [executable, "--stop-server"],
        check=False,
        capture_output=True,
        env=environment,
        timeout=15,
    )
    end_process(process, timeout=_STOP_TIMEOUT_SECONDS)


def _server_pid(environment: cabc.Mapping[str, str]) -> int:
    """Read the server PID from Linux peer credentials on its private socket.

    Returns
    -------
    int
        The peer process ID reported by ``SO_PEERCRED``.
    """
    socket_path = environment["SCCACHE_SERVER_UDS"]
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(socket_path)
        peer = connection.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
    process_id, _user_id, _group_id = struct.unpack("3i", peer)
    return process_id


def _compile_command(
    executable: str,
    source: Path,
    output: Path,
    metadata: str,
) -> list[str]:
    """Build one supported library request with a distinct metadata key.

    Returns
    -------
    list[str]
        A complete sccache client argv vector.
    """
    return [
        executable,
        "rustc",
        "--crate-name",
        "fixture",
        "--crate-type=rlib",
        "--edition=2021",
        "--emit=link",
        "-C",
        f"metadata={metadata}",
        "--out-dir",
        str(output.parent),
        str(source),
    ]


def _run_compile(
    command: cabc.Sequence[str],
    environment: cabc.Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run one compile request and return its client-visible result.

    Returns
    -------
    subprocess.CompletedProcess[str]
        The compile request's captured output and exit status.
    """
    # ruff: ignore[subprocess-without-shell-equals-true] - caller argv, no shell.
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )


def sccache_host_marks() -> list[pytest.MarkDecorator]:
    """Mark a Linux-only opt-in test and skip hosts without the pinned binary.

    Example
    -------
    Use ``pytestmark = sccache_host_marks()`` in a host-check module.

    Returns
    -------
    list[pytest.MarkDecorator]
        The opt-in and version-availability marks for the host check.
    """
    check = HostCheck(
        marker=pytest.mark.sccache_host_check,
        opt_in_variable="BUILD_LIMITS_SCCACHE_HOST_CHECK",
        binary="sccache",
        versions=SUPPORTED_SCCACHE_VERSIONS,
        unavailable_reason=_UNSUPPORTED_VERSION_REASON,
        version_error_reason=_VERSION_ERROR_REASON,
    )
    return host_check_marks(
        check,
        platform_reason=_LINUX_REASON,
        opt_in_reason=_OPT_IN_REASON,
    )
