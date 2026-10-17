"""A stand-in for sccache with the same process model, for supervisor tests.

Real sccache compiles a cache miss as a direct child of its server process,
outside the client's control group, and the client waits for the server's
reply. This fake keeps exactly that shape without needing a real compiler:

- ``fake_sccache.py server`` listens on ``SCCACHE_SERVER_UDS``. For each
  connection it reads one JSON argument vector, runs it as its own child with
  the client's working directory, and replies with the exit status and the
  captured output, as sccache relays a cache miss's output to its client.
- ``fake_sccache.py client <compiler> <args...>`` sends the vector, writes the
  relayed output to its own streams and exits with the server's status. If
  no server answers it runs the compiler itself, as sccache does with
  ``SCCACHE_IGNORE_SERVER_IO_ERROR``, and never starts a server of its own.

The client may also name a jobserver FIFO and a ready marker, mirroring the
point at which real sccache blocks: upstream acquires a jobserver slot before
it spawns the compiler, and accepted work survives the client's disconnect.
The server writes the ready marker once the request is accepted, blocks
reading one byte from the FIFO, and returns the byte only after the child
exits. Tests that leave the FIFO unread therefore reproduce a detached
pre-spawn wait with no visible compiler; tests that do not name a FIFO keep
the direct behaviour above.
"""

import contextlib
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def serve(path: str) -> None:
    """Serve compile requests until killed."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(64)
    while True:
        conn, _ = listener.accept()
        threading.Thread(target=_handle, args=(conn,), daemon=True).start()


def _handle(conn: socket.socket) -> None:
    """Run one request as a direct child and reply with its result.

    The work is completed even when the client disconnects first, as the real
    server completes accepted work. A reply the client no longer reads is
    discarded rather than treated as a server fault.
    """
    with conn, conn.makefile("rwb") as stream:
        request = json.loads(stream.readline())
        _announce(request.get("ready"))
        _acquire(request.get("jobserver"))
        try:
            child = subprocess.run(
                request["argv"], cwd=request["cwd"], capture_output=True, check=False
            )
        finally:
            _release(request.get("jobserver"))
        reply = {
            "code": child.returncode,
            "stdout": child.stdout.decode("latin-1"),
            "stderr": child.stderr.decode("latin-1"),
        }
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            stream.write(json.dumps(reply).encode() + b"\n")
            stream.flush()


def _announce(ready: str | None) -> None:
    """Write the ready marker once the request has been accepted."""
    if ready:
        pathlib.Path(ready).write_text("ready\n", encoding="ascii")


def _acquire(jobserver: str | None) -> None:
    """Block until the jobserver FIFO supplies a slot, as upstream does.

    A missing FIFO path means the direct behaviour: work starts immediately.
    Once the FIFO is open the request has reached the wait, so it writes a
    ``<jobserver>.waiting`` marker before the blocking read; a test that never
    writes a slot can then prove the pre-spawn state without a timeout.
    """
    if not jobserver:
        return
    with pathlib.Path(jobserver).open("rb", buffering=0) as fifo:
        pathlib.Path(f"{jobserver}.waiting").write_text("waiting\n", encoding="ascii")
        fifo.read(1)


def _release(jobserver: str | None) -> None:
    """Return the jobserver slot so a following request is not starved.

    The FIFO is opened read-write and non-blocking: a write-only open would
    block with no reader present, hanging a ``finally`` that must run even
    when the compiler never spawned.
    """
    if not jobserver:
        return
    descriptor = os.open(jobserver, os.O_RDWR | os.O_NONBLOCK)
    try:
        os.write(descriptor, b"+")
    finally:
        os.close(descriptor)


def request_fields(env: cabc.Mapping[str, str]) -> dict[str, str]:
    """Collect the optional detached-work fields for one client request.

    Parameters
    ----------
    env : Mapping[str, str]
        The client's environment.

    Returns
    -------
    dict[str, str]
        ``jobserver`` and ``ready`` paths, each present only when configured.
    """
    fields = {}
    if jobserver := env.get("FAKE_SCCACHE_JOBSERVER"):
        fields["jobserver"] = jobserver
    if ready := env.get("FAKE_SCCACHE_READY"):
        fields["ready"] = ready
    return fields


def client(argv: list[str], env: cabc.Mapping[str, str] | None = None) -> int:
    """Send one request, falling back to a local run without a server.

    Parameters
    ----------
    argv : list[str]
        The compiler's argument vector.
    env : Mapping[str, str] | None
        The environment naming optional detached-work fields.

    Returns
    -------
    int
        The compilation's exit status.
    """
    env = os.environ if env is None else env
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(env["SCCACHE_SERVER_UDS"])
    except KeyError, OSError:
        return subprocess.run(argv, check=False).returncode
    with conn, conn.makefile("rwb") as stream:
        request = {"argv": argv, "cwd": str(pathlib.Path.cwd())}
        request.update(request_fields(env))
        stream.write(json.dumps(request).encode() + b"\n")
        stream.flush()
        reply = json.loads(stream.readline())
    sys.stdout.buffer.write(reply["stdout"].encode("latin-1"))
    sys.stderr.buffer.write(reply["stderr"].encode("latin-1"))
    return int(reply["code"])


if __name__ == "__main__":
    if sys.argv[1] == "server":
        serve(os.environ["SCCACHE_SERVER_UDS"])
    sys.exit(client(sys.argv[2:]))
