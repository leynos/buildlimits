"""A stand-in for kache with the same process model, for supervisor tests.

Real kache wraps the compiler and runs it as a child of the wrapper, inside the
compiler group's own scope, while consulting a separately managed daemon over a
socket. This fake keeps exactly that shape without a real compiler or daemon:

- ``fake_kache.py daemon run`` binds ``KACHE_SOCKET_PATH`` and accepts
  connections until it is killed. Nothing is served over them; the socket
  answering is the only evidence the supervisor's probe needs.
- ``fake_kache.py daemon status --json`` prints the documented status document,
  reporting a running daemon only while that socket answers, so a stopped
  daemon reads as unavailable exactly as the real one does.
- ``fake_kache.py <compiler> <args...>`` runs the compiler as its own child,
  in-process, and relays its streams and exit status. The child therefore
  stays inside the caller's control group, as the real wrapper's does.

An optional leak mode starts a long-lived child before compiling and records
its process identifier -- but only once the child's executable link is named
kache, so a marker always names the evidence the guard reads rather than a
process that merely exists.
"""

import contextlib
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import time
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: The probe must not hang when a daemon accepts the connection and stalls.
SOCKET_TIMEOUT_SECONDS = 2.0

#: How long the leak mode waits for its child to exec the named executable.
NAME_DEADLINE_SECONDS = 5.0


def serve(path: str) -> None:
    """Bind the daemon's socket and accept connections until killed.

    Parameters
    ----------
    path : str
        The socket to bind. A connect succeeding on it is the whole of the
        health evidence, so nothing is read or written on the connections.
    """
    with contextlib.suppress(OSError):
        pathlib.Path(path).unlink()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(path)
        listener.listen(64)
        while True:
            conn, _ = listener.accept()
            conn.close()


def is_running(path: str | None) -> bool:
    """Report whether a daemon answers on the given socket.

    Parameters
    ----------
    path : str or None
        The socket named by ``KACHE_SOCKET_PATH``, or ``None`` when unset.

    Returns
    -------
    bool
        Whether a connection succeeded.

    Examples
    --------
    >>> is_running(None)
    False
    >>> is_running("/nonexistent/kache.sock")
    False
    """
    if not path:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(SOCKET_TIMEOUT_SECONDS)
            probe.connect(path)
    except OSError:
        return False
    return True


#: The envelope kache publishes for every ``--json`` command. Health checks
#: it, so a fixture that omitted it would not exercise the contract the
#: supervisor actually reads.
SCHEMA_VERSION = 1
COMMAND = "daemon-status"


def status(path: str | None) -> int:
    """Print the status document the supervisor's health probe reads.

    The real command reports absence in the document rather than in its exit
    status, which is what lets the supervisor poll it on every admission cycle
    without treating a stopped daemon as a probe failure. The body is therefore
    wrapped in the versioned envelope kache publishes for every ``--json``
    command: ``success`` describes the command, not the daemon, and stays true
    whether or not one is running.

    Parameters
    ----------
    path : str or None
        The socket the probe named, echoed back so a supervisor can reject a
        daemon answering on a different endpoint.

    Returns
    -------
    int
        The exit status, always zero.
    """
    document: dict[str, typ.Any] = {
        "schema_version": SCHEMA_VERSION,
        "command": COMMAND,
        "success": True,
        "daemon_running": is_running(path),
    }
    if path:
        document["socket"] = path
    sys.stdout.write(json.dumps(document) + "\n")
    sys.stdout.flush()
    return 0


def compile_with(argv: list[str], env: cabc.Mapping[str, str]) -> int:
    """Run the compiler as the wrapper's own child and relay its result.

    Parameters
    ----------
    argv : list[str]
        The compiler's argument vector.
    env : Mapping[str, str]
        The wrapper's environment, which may switch on the leak mode.

    Returns
    -------
    int
        The compiler's exit status.
    """
    _leak_daemon(env)
    return subprocess.run(argv, check=False).returncode


def _leak_daemon(env: cabc.Mapping[str, str]) -> None:
    """Start a long-lived child and record the evidence the guard reads.

    The child stays in the wrapper's control group, which is what lets a test
    prove that cancelling the group removes a daemon a client started. Its
    session is irrelevant to that: the guard walks the compiler slice and then
    ``/proc``, and neither follows sessions.

    The wrapper writes the marker only once the child's executable link names
    ``kache``, so the recorded identifier is evidence rather than a promise.
    It makes that observation because it is the child's parent -- the one
    process guaranteed to be able to see it. A test reading the marker from
    outside is not: a hosted CI runner does not reliably keep a grandchild
    observable for the length of the test that started it, and a test that
    races that policy passes on a developer's machine and fails on every
    hosted run. See the developers' guide, §11.5.

    Parameters
    ----------
    env : Mapping[str, str]
        The wrapper's environment. ``FAKE_KACHE_LEAK_DAEMON`` names the pid
        file, and its absence turns the mode off; ``FAKE_KACHE_LEAK_BINARY``
        names the executable to leave behind.
    """
    marker = env.get("FAKE_KACHE_LEAK_DAEMON")
    if not marker:
        return
    binary = env.get("FAKE_KACHE_LEAK_BINARY", "/bin/sleep")
    # The leak is the point: the child must outlive this wrapper so that the
    # group's cancellation is what removes it, so no block may await it here.
    # It is a plain child: detaching it into its own session would model
    # nothing the guard reads, and would make the observation unreliable.
    child = subprocess.Popen(  # pylint: disable=consider-using-with
        [binary, "300"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _await_own_name(child.pid, pathlib.Path(binary).name)
    pathlib.Path(marker).write_text(f"{child.pid}\n", encoding="ascii")


def _await_own_name(pid: int, wanted: str) -> None:
    """Wait until *pid*'s executable link reports *wanted*.

    A freshly forked child still links to the interpreter its parent ran; the
    link names the new program only after ``execve`` succeeds. Waiting here is
    what makes the marker's identifier mean "a process presenting this name
    was started", rather than "a process identifier was allocated".

    Parameters
    ----------
    pid : int
        The child to read.
    wanted : str
        The executable name the guard matches.

    Raises
    ------
    RuntimeError
        If the child never presents the name, which means the leak mode failed
        to produce the evidence the guard reads. Reporting it through this
        fixture's exit status keeps the failure where it happened, instead of
        leaving a test to infer it from a process it cannot see.
    """
    # A bounded wait: a child that cannot start must fail the leak mode rather
    # than hang the wrapper a scenario is driving.
    deadline = time.monotonic() + NAME_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError):
            if pathlib.Path(f"/proc/{pid}/exe").resolve().name == wanted:
                return
        time.sleep(0.01)
    # The child never became the named process, so nothing the guard looks for
    # exists. Remove it rather than leave a stray sleep behind.
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)
    msg = f"the leaked child never presented the executable name {wanted!r}"
    raise RuntimeError(msg)


def main(argv: list[str], env: cabc.Mapping[str, str]) -> int:
    """Dispatch one fake-kache invocation.

    Parameters
    ----------
    argv : list[str]
        The arguments after the program name.
    env : Mapping[str, str]
        The wrapper's environment.

    Returns
    -------
    int
        The exit status.
    """
    if argv[:2] == ["daemon", "status"]:
        return status(env.get("KACHE_SOCKET_PATH"))
    if argv[:2] == ["daemon", "run"]:
        serve(env["KACHE_SOCKET_PATH"])
        return 0
    if not argv:
        sys.stderr.write("fake kache: no compiler given\n")
        return 2
    return compile_with(argv, env)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
