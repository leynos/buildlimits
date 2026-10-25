"""Support the opt-in real-sccache acceptance checks."""

import json
import os
import shutil
import socket
import struct

# ruff: ignore[suspicious-subprocess-import] - fixed argv, no shell.
import subprocess
import sys
import time
import typing as typ
from contextlib import contextmanager

import pytest
from sccache_stats import _stats_payload

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

_WINDOW_COUNT = 200


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
    TypeError
        If the JSON value or its ``stats`` member is not an object.
    ValueError
        If the statistics document reports another sccache version.
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
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        message = "sccache returned a non-object statistics document"
        raise TypeError(message)
    document = typ.cast("dict[str, object]", value)
    if document.get("version") != "0.14.0":
        message = "sccache returned statistics for an unexpected version"
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
                    yield process, prefix is not None
                    break
            else:
                pytest.fail(f"sccache server did not become ready; see {log_path}")
        finally:
            # ruff: ignore[subprocess-without-shell-equals-true] - stop argv, no shell.
            subprocess.run(
                [executable, "--stop-server"],
                check=False,
                capture_output=True,
                env=environment,
                timeout=15,
            )
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


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
    marks = [pytest.mark.sccache_host_check, pytest.mark.timeout(600)]
    if sys.platform != "linux":
        marks.append(
            pytest.mark.skipif(
                condition=True,
                reason="the host check requires Linux /proc and SO_PEERCRED",
            )
        )
        return marks

    enabled = os.environ.get("BUILD_LIMITS_SCCACHE_HOST_CHECK") == "1"
    if not enabled:
        marks.append(
            pytest.mark.skipif(
                condition=True,
                reason="set BUILD_LIMITS_SCCACHE_HOST_CHECK=1 to run the host check",
            )
        )
        return marks

    executable = shutil.which("sccache")
    if executable is None:
        reason = "sccache is not installed"
    else:
        try:
            # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv, no shell.
            version = subprocess.run(
                [executable, "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except OSError, subprocess.TimeoutExpired:
            reason = "could not verify the sccache version"
        else:
            reason = (
                "the host check requires sccache 0.14.0"
                if version.returncode != 0 or version.stdout.strip() != "sccache 0.14.0"
                else ""
            )
    if reason:
        marks.append(pytest.mark.skipif(condition=True, reason=reason))
    return marks
