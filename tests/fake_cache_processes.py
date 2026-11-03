"""Start and stop the fake cache stand-ins that serve a socket.

Both stand-ins, :mod:`fixtures.fake_sccache` and :mod:`fixtures.fake_kache`,
announce readiness the same way: they bind the socket they were given and then
serve it. Both also own a process tree, so a test that has finished with one
has the same two duties. Starting and stopping are therefore one pair, and
they live here rather than in :mod:`harness_processes`, which holds the
general-purpose process observations the whole harness shares.
"""

import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed fixture argv, never a shell.
import time
import typing as typ

from harness_processes import reap_and_close_pipes

if typ.TYPE_CHECKING:
    from pathlib import Path


def kill_and_reap(process: subprocess.Popen[typ.Any], timeout: float = 30.0) -> None:
    """Kill a backend stand-in and release everything held on it.

    Ending a stand-in outright is not enough on its own: the pipes the parent
    holds stay open after the process dies, and a descriptor left to the
    collector is handed to a later test.

    Parameters
    ----------
    process : subprocess.Popen
        The stand-in to end.
    timeout : float
        Seconds to allow it to exit.

    Examples
    --------
    Stop a fixture once its probe is done::

        kill_and_reap(process, timeout=10)
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

    Waiting here means a scenario never races the bind: an assertion made
    immediately after this returns is made against a fixture that is already
    answering.

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

    Examples
    --------
    Start a fake cache and stop it after the probe::

        process = start_bound(argv, env, socket, stderr=subprocess.DEVNULL)
        try:
            probe(socket)
        finally:
            kill_and_reap(process, timeout=10)
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
