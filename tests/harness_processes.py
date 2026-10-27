"""Owned harness subprocess checks; unit and token authority stay in Harness."""

import contextlib
import json
import os
import resource
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed fixture argv, never a shell.
import time
import typing as typ
from pathlib import Path

from buildlimits.systemd import run


def journal(unit: str, lines: int = 60) -> str:
    """Return a unit's recent journal, for failure messages.

    Parameters
    ----------
    unit : str
        The unit whose journal is wanted.
    lines : int
        How many recent lines to return.

    Returns
    -------
    str
        Recent journal lines, oldest first.
    """
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv, no shell.
        [
            "/usr/bin/journalctl",
            "--user",
            "-u",
            unit,
            "--no-pager",
            "-o",
            "cat",
            "-n",
            str(lines),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    ).stdout


def journal_records(unit: str, lines: int = 200) -> list[dict[str, typ.Any]]:
    """Return a unit's recent journal as decoded structured records.

    :func:`journal` returns messages only. A scenario that has to read a
    record's own fields needs the structured form, because the supervisor's
    metric transport is trust gated on the installed unit name and a harness
    unit deliberately carries a different one.

    Parameters
    ----------
    unit : str
        The unit whose journal is wanted.
    lines : int
        How many recent records to return.

    Returns
    -------
    list[dict[str, Any]]
        One decoded record per journal line, oldest first. A line that is not
        a JSON object is skipped rather than failing the read.
    """
    text = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv, no shell.
        [
            "/usr/bin/journalctl",
            "--user",
            "-u",
            unit,
            "--no-pager",
            "-o",
            "json",
            "-n",
            str(lines),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    ).stdout
    records: list[dict[str, typ.Any]] = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def pid_alive(pid: int) -> bool:
    """Whether a process exists.

    Parameters
    ----------
    pid : int
        The process.

    Returns
    -------
    bool
        Whether it exists and is not a zombie.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    with contextlib.suppress(OSError):
        return stat.read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0] != "Z"
    return False


def kill_group(process: subprocess.Popen[bytes], number: int = signal.SIGKILL) -> None:
    """Kill an adapter's whole process group, as an agent's interruption would.

    Parameters
    ----------
    process : subprocess.Popen[bytes]
        The adapter, started in its own session.
    number : int
        The signal.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, number)


def reap_and_close_pipes(
    process: subprocess.Popen[typ.Any], timeout: float = 30.0
) -> None:
    """Wait for a killed or finished process, then close the pipes we hold.

    Reaping does not close the pipes the parent holds; leaving them to the
    collector hands their descriptors to a later test.

    Parameters
    ----------
    process : subprocess.Popen
        A process that has been killed or is expected to finish.
    timeout : float
        Seconds to wait for it to end. A process that outlives this still has
        its pipes closed.
    """
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=timeout)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def kill_and_reap(process: subprocess.Popen[typ.Any], timeout: float = 30.0) -> None:
    """Kill a backend stand-in and release everything held on it.

    Both fixtures serve a socket and own a process tree, so a test that has
    finished with one has the same two duties: end it outright and reap it, or
    the descriptor it was started with leaks into the next test.

    Parameters
    ----------
    process : subprocess.Popen
        The stand-in to end.
    timeout : float
        Seconds to allow it to exit.
    """
    process.kill()
    reap_and_close_pipes(process, timeout)


def start_bound(
    argv: list[str],
    env: dict[str, str],
    socket_path: Path,
    *,
    stderr: int | None,
) -> subprocess.Popen[bytes]:
    """Start a fixture process and wait for the socket it binds.

    Both cache stand-ins announce readiness the same way, by binding the
    socket they were given, and both have to be stopped by the caller once
    its probe is done. Waiting here means a scenario never races the bind.

    Parameters
    ----------
    argv : list[str]
        The fixture's own command line.
    env : dict[str, str]
        The environment the fixture reads its socket from.
    socket_path : Path
        The socket it is expected to bind.
    stderr : int | None
        Where the fixture's standard error goes.

    Returns
    -------
    subprocess.Popen[bytes]
        The running fixture, which the caller still has to
        :func:`kill_and_reap`.

    Raises
    ------
    AssertionError
        If it never binds the socket; it is killed and reaped first.
    """
    # The handle outlives this helper on purpose: the caller probes the
    # fixture and then stops it.
    process = subprocess.Popen(  # pylint: disable=consider-using-with  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv, no shell.
        argv, env=env, stdout=subprocess.DEVNULL, stderr=stderr
    )
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if socket_path.exists():
            return process
        time.sleep(0.02)
    kill_and_reap(process, timeout=10)
    msg = "the fixture never bound its socket"
    raise AssertionError(msg)


def server_children(unit: str, socket_path: Path) -> int:
    """Count compilations a fake sccache server is running.

    Parameters
    ----------
    unit : str
        The managed cache's unit name.
    socket_path : Path
        The socket the server was configured to bind.

    Returns
    -------
    int
        Direct children of the server process.
    """
    from buildlimits.sccache import ManagedSccache

    return ManagedSccache(unit, socket_path).active_compiles()


def manager_nofile_limit(requested: int = 524288) -> int:
    """Select a fixture hard limit reachable by the current user manager.

    Parameters
    ----------
    requested : int
        Desired hard descriptor limit for the isolated supervisor.

    Returns
    -------
    int
        Desired limit capped by the manager's actual kernel resource limit.

    Raises
    ------
    RuntimeError
        If the user manager has no running process.
    """
    result = run(
        [
            "/usr/bin/systemctl",
            "show",
            f"user@{os.getuid()}.service",
            "--property=MainPID",
            "--value",
        ],
    )
    result.check_returncode()
    pid = int(result.stdout.strip())
    if pid <= 0:
        msg = "pressure tests require a running user manager"
        raise RuntimeError(msg)
    _, hard = resource.prlimit(pid, resource.RLIMIT_NOFILE)
    return requested if hard == resource.RLIM_INFINITY else min(requested, hard)
