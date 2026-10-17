"""The sccache stand-in reproduces the detached pre-spawn wait.

Issue #285 recorded an interrupted cached build leaving every shared token in
``server-drain`` with no visible compiler processes. The real server blocks
acquiring a jobserver slot before it spawns a compiler, and accepted work
survives the client's disconnect, so the fixture must be able to reproduce
that state: a request that is accepted, announced by a ready marker, blocked
on a jobserver FIFO, and left behind when its client goes away.
"""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import typing as typ
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "fake_sccache.py"


class Reply(typ.TypedDict):
    """The fixture server's reply fields."""

    code: int
    stdout: str
    stderr: str


_SPEC = importlib.util.spec_from_file_location("fake_sccache", FIXTURE)
assert _SPEC is not None and _SPEC.loader is not None, (
    "the fixture module must be loadable by path"
)
fake_sccache = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fake_sccache)


def _wait_for_content(path: Path, expected: str, timeout: float = 10.0) -> None:
    """Wait until a fixture file holds exactly the content a test asserts on.

    A bare existence check is not enough. A fixture child writes with
    ``Path.write_text``, which publishes the name at ``open`` and its bytes at
    ``close``, so a reader that returns on ``exists`` can observe an empty
    file and fail an assertion on content that was about to arrive. Waiting on
    the content itself removes that window.

    Parameters
    ----------
    path : Path
        The file the fixture writes.
    expected : str
        The exact content the caller is about to assert on.
    timeout : float
        How long to keep polling before failing.

    Raises
    ------
    AssertionError
        If the file never holds the expected content in time.
    """
    deadline = time.monotonic() + timeout
    seen: str | None = None
    while time.monotonic() < deadline:
        try:
            seen = path.read_text(encoding="ascii")
        except FileNotFoundError:
            seen = None
        if seen == expected:
            return
        time.sleep(0.02)
    msg = f"{path} never held {expected!r}; last content was {seen!r}"
    raise AssertionError(msg)


def _server(socket_path: Path) -> subprocess.Popen[bytes]:
    """Start the fixture server on a private socket."""
    env = {**os.environ, "SCCACHE_SERVER_UDS": str(socket_path)}
    # The caller keeps the server alive across a client disconnect and kills it
    # in a finally block, so the handle deliberately outlives this helper.
    process = subprocess.Popen(  # pylint: disable=consider-using-with
        [sys.executable, str(FIXTURE), "server"], env=env, stderr=subprocess.PIPE
    )
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if socket_path.exists():
            return process
        time.sleep(0.02)
    msg = "the fixture server never bound its socket"
    raise AssertionError(msg)


def _send(socket_path: Path, request: dict[str, object]) -> Reply:
    """Send one request and read its reply."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(str(socket_path))
        with conn.makefile("rwb") as stream:
            stream.write(json.dumps(request).encode() + b"\n")
            stream.flush()
            return json.loads(stream.readline())


def test_request_fields_are_only_present_when_configured() -> None:
    """A plain client keeps the direct shape, so existing tests are unaffected."""
    assert fake_sccache.request_fields({}) == {}, "no detached-work fields by default"
    fields = fake_sccache.request_fields({
        "FAKE_SCCACHE_JOBSERVER": "/run/jobserver",
        "FAKE_SCCACHE_READY": "/run/ready",
    })
    assert fields == {"jobserver": "/run/jobserver", "ready": "/run/ready"}, (
        "configured paths are carried into the request"
    )


def test_accepted_work_survives_client_disconnect_and_waits_for_a_jobserver_slot(
    tmp_path: Path,
) -> None:
    """The fixture blocks before spawning the compiler and outlives its client."""
    socket_path = tmp_path / "sccache.sock"
    ready = tmp_path / "ready"
    completed = tmp_path / "completed"
    jobserver = tmp_path / "jobserver.fifo"
    os.mkfifo(jobserver)
    # The holder keeps the FIFO readable across the client's close, so the
    # server's blocking read is released only by the slot this test writes.
    holder = os.open(jobserver, os.O_RDWR)
    server = _server(socket_path)
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        stream = client.makefile("rwb")
        stream.write(
            json.dumps({
                "argv": [
                    sys.executable,
                    "-c",
                    (
                        "import pathlib, sys; "
                        "pathlib.Path(sys.argv[1]).write_text('done', encoding='ascii')"
                    ),
                    str(completed),
                ],
                "cwd": str(tmp_path),
                "jobserver": str(jobserver),
                "ready": str(ready),
            }).encode()
            + b"\n"
        )
        stream.flush()
        # The waiting marker is written after the FIFO opens, immediately
        # before the blocking read, so no compiler child can exist yet.
        _wait_for_content(Path(f"{jobserver}.waiting"), "waiting\n")
        # The client goes away while the work is still pre-spawn.
        stream.close()
        client.close()
        assert not _children(server.pid), "no compiler child exists while blocked"
        # The detached request completes once a slot arrives, with no client
        # left to collect the result.
        os.write(holder, b"+")
        _wait_for_content(completed, "done")
        assert completed.read_text(encoding="ascii") == "done", (
            "the detached request compiled after its client left"
        )
        reply = _send(
            socket_path,
            {"argv": [sys.executable, "-c", "print('second')"], "cwd": str(tmp_path)},
        )
        assert reply["code"] == 0, "the server survives the disconnect"
        assert "second" in reply["stdout"], "later requests still work"
    finally:
        os.close(holder)
        server.kill()
        server.wait(timeout=10)


def test_a_result_is_read_only_after_the_child_finishes_writing(
    tmp_path: Path,
) -> None:
    """A published-but-empty file is not a finished result.

    The fixture's children write their result with ``Path.write_text``, which
    creates the file at ``open`` and fills it at ``close``, so a wait that
    returns on the file's existence can read the empty window the write
    opened — the race that made the disconnect test fail on CI with
    ``assert '' == 'done'``. Here the child holds that window open for a
    second, so the claim is checked without racing a scheduler.
    """
    socket_path = tmp_path / "sccache.sock"
    completed = tmp_path / "completed"
    server = _server(socket_path)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(str(socket_path))
        with client.makefile("rwb") as stream:
            stream.write(
                json.dumps({
                    "argv": [
                        sys.executable,
                        "-c",
                        (
                            "import pathlib, sys, time; "
                            "p = pathlib.Path(sys.argv[1]); "
                            "p.write_text('', encoding='ascii'); "
                            "time.sleep(1.0); "
                            "p.write_text('done', encoding='ascii')"
                        ),
                        str(completed),
                    ],
                    "cwd": str(tmp_path),
                }).encode()
                + b"\n"
            )
            stream.flush()
        _wait_for_content(completed, "done")
        assert completed.read_text(encoding="ascii") == "done", (
            "the wait observed the finished write, not the empty window"
        )
    finally:
        client.close()
        server.kill()
        server.wait(timeout=10)


def _children(pid: int) -> list[int]:
    """List the fixture server's direct children, as a diagnostic only."""
    result = subprocess.run(
        ["pgrep", "--parent", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    return [int(line) for line in result.stdout.split() if line.strip()]
