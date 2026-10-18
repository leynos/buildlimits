"""Owned harness subprocess checks; unit and token authority stay in Harness."""

import contextlib
import os
import resource
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed fixture argv, never a shell.
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
