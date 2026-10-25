"""Owned harness subprocess checks; unit and token authority stay in Harness."""

import contextlib
import dataclasses as dc
import os
import resource
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed fixture argv, never a shell.
import time
import typing as typ
from pathlib import Path

from buildlimits.systemd import run

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def wait_for(condition: cabc.Callable[[], bool], timeout: float = 15.0) -> bool:
    """Poll until a condition holds or the timeout expires.

    Parameters
    ----------
    condition : Callable[[], bool]
        The condition to poll.
    timeout : float
        The maximum wait in seconds.

    Returns
    -------
    bool
        Whether the condition held before the deadline.

    Examples
    --------
    Wait for a process marker to appear::

        if wait_for(marker.exists, timeout=2):
            read_marker()
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


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


def own_process(
    owned: contextlib.ExitStack, process: subprocess.Popen[bytes]
) -> subprocess.Popen[bytes]:
    """Register process-group cleanup and pipe reaping on an ownership stack.

    Parameters
    ----------
    owned : ExitStack
        Stack that owns the process cleanup callbacks.
    process : subprocess.Popen
        Process started in its own session with optional captured pipes.

    Returns
    -------
    subprocess.Popen
        The same process, for use at its call site.

    Examples
    --------
    Register a harness process immediately after starting it::

        process = own_process(owned, harness.invoke(argv, piped=True))
    """
    owned.callback(reap_and_close_pipes, process)
    owned.callback(_kill_if_running, process)
    return process


def _kill_if_running(process: subprocess.Popen[bytes]) -> None:
    """Kill a still-running process group before its pipes are reaped."""
    if process.poll() is None:
        kill_group(process)


def reap_and_close_pipes(
    process: subprocess.Popen[bytes] | subprocess.Popen[str], timeout: float = 30.0
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


@dc.dataclass(frozen=True, slots=True)
class AdapterProcessOptions:
    """Common process settings for an adapter invocation."""

    cwd: Path
    base_env: cabc.Mapping[str, str]
    piped: bool = False
    env: cabc.Mapping[str, str] | None = None


def start_adapter(
    adapter: Path, argv: cabc.Sequence[str], options: AdapterProcessOptions
) -> subprocess.Popen[bytes]:
    """Start an adapter with the host harness's standard process setup.

    Parameters
    ----------
    adapter : Path
        The adapter executable.
    argv : Sequence[str]
        Arguments passed to the adapter without rewriting.
    options : AdapterProcessOptions
        Working directory, inherited environment and optional pipe settings.

    Returns
    -------
    subprocess.Popen[bytes]
        The process in a fresh session.

    Examples
    --------
    Start a version probe with captured streams, then inspect its result::

        process = start_adapter(
            adapter,
            ("rustc", "-vV"),
            AdapterProcessOptions(cwd=work, base_env=harness.env, piped=True),
        )
        stdout, stderr = process.communicate(timeout=2)
    """
    stream = subprocess.PIPE if options.piped else None
    caller = (
        dict(options.base_env)
        if options.env is None
        else {**options.base_env, **options.env}
    )
    return subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true] - argv is an explicit test harness command.
        [str(adapter), *argv],
        cwd=options.cwd,
        env=caller,
        start_new_session=True,
        stdin=stream,
        stdout=stream,
        stderr=stream,
        text=False,
    )


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
