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
its process identifier, so a test can prove what the supervisor does with a
daemon a client leaves behind.
"""

import contextlib
import json
import os
import pathlib
import socket
import subprocess
import sys
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: The probe must not hang when a daemon accepts the connection and stalls.
SOCKET_TIMEOUT_SECONDS = 2.0


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


def status(path: str | None) -> int:
    """Print the status document the supervisor's health probe reads.

    The real command reports absence in the document rather than in its exit
    status, which is what lets the supervisor poll it on every admission cycle
    without treating a stopped daemon as a probe failure.

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
    document: dict[str, typ.Any] = {"daemon_running": is_running(path)}
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
    """Start a long-lived child and record its process identifier.

    The child stays in the wrapper's control group, which is what lets a test
    prove that cancelling the group removes a daemon a client started. It is
    detached from the wrapper's session so that it survives the wrapper's own
    exit, as a daemon does.

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
    child = subprocess.Popen(  # pylint: disable=consider-using-with
        [binary, "300"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pathlib.Path(marker).write_text(f"{child.pid}\n", encoding="ascii")


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
