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
- ``fake_sccache.py client --show-stats --stats-format json`` returns the
  pinned 0.14.0 statistics fixture for the outcome-query adapter.

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
import time
import typing as typ

from fake_sccache_statistics import (
    COUNTER_NAMES as _COUNTER_NAMES,
)
from fake_sccache_statistics import (
    STATE_LOCK as _STATE_LOCK,
)
from fake_sccache_statistics import (
    ServerState,
)
from fake_sccache_statistics import (
    request_statistics as _request_statistics,
)
from fake_sccache_statistics import (
    statistics as _statistics,
)
from fake_sccache_statistics import (
    statistics_delay as _statistics_delay_impl,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_STATISTICS_ARGUMENTS = ("--show-stats", "--stats-format", "json")


def serve(path: str) -> None:
    """Serve compile requests until killed."""
    state = _load_state(path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(64)
    while True:
        conn, _ = listener.accept()
        threading.Thread(target=_handle, args=(conn, state, path), daemon=True).start()


def _handle(conn: socket.socket, state: ServerState, socket_path: str) -> None:
    """Run one request as a direct child and reply with its result.

    The work is completed even when the client disconnects first, as the real
    server completes accepted work. A reply the client no longer reads is
    discarded rather than treated as a server fault.
    """
    with conn, conn.makefile("rwb") as stream:
        request = json.loads(stream.readline())
        if request.get("operation") == "statistics":
            delay = _statistics_delay(socket_path, state)
            if delay > 0:
                time.sleep(delay)
            with _STATE_LOCK:
                counters = state["counters"].copy()
            reply = {"version": "0.14.0", "stats": _statistics(counters)}
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                stream.write(json.dumps(reply).encode() + b"\n")
                stream.flush()
            return
        _announce(request.get("ready"))
        result = _next_result(socket_path, state)
        if result == "hit":
            _sleep_for_configured_result(socket_path, "hit_delay_seconds")
            child = subprocess.CompletedProcess(request["argv"], 0, b"", b"")
        else:
            _sleep_for_configured_result(socket_path, "miss_delay_seconds")
            _acquire(request.get("jobserver"))
            try:
                child = subprocess.run(
                    request["argv"],
                    cwd=request["cwd"],
                    capture_output=True,
                    check=False,
                    env=_compiler_environment(socket_path),
                )
            finally:
                _release(request.get("jobserver"))
        _record_result(socket_path, state, result, child.returncode)
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


def _sleep_for_configured_result(socket_path: str, field: str) -> None:
    """Delay one scripted response without persisting request arguments."""
    delay = _configuration(socket_path).get(field, 0.0)
    if isinstance(delay, (int, float)) and delay > 0:
        time.sleep(float(delay))


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


def _load_state(socket_path: str) -> ServerState:
    """Load persistent counters so a service restart preserves history.

    Returns
    -------
    ServerState
        The last saved counters, or an empty state after invalid data.
    """
    path = pathlib.Path(socket_path).with_name("fake-sccache-state.json")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        counters = {name: int(raw["counters"].get(name, 0)) for name in _COUNTER_NAMES}
        return {
            "counters": counters,
            "script_index": int(raw.get("script_index", 0)),
            "statistics_queries": int(raw.get("statistics_queries", 0)),
        }
    except OSError, ValueError, TypeError, KeyError:
        return {
            "counters": dict.fromkeys(_COUNTER_NAMES, 0),
            "script_index": 0,
            "statistics_queries": 0,
        }


def _save_state(socket_path: str, state: ServerState) -> None:
    """Persist counters and script position for tests and service restarts."""
    path = pathlib.Path(socket_path).with_name("fake-sccache-state.json")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _configuration(socket_path: str) -> dict[str, object]:
    """Read the harness-owned scripted result and statistics delay.

    Returns
    -------
    dict[str, object]
        The fake cache options, or an empty mapping after invalid data.
    """
    path = pathlib.Path(socket_path).with_name("fake-sccache-config.json")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError, TypeError:
        return {}
    return value if isinstance(value, dict) else {}


def _compiler_environment(socket_path: str) -> dict[str, str]:
    """Apply harness-owned fake compiler delays to a server-side child.

    Returns
    -------
    dict[str, str]
        The fake server environment with only configured test controls added.
    """
    environment = os.environ.copy()
    configuration = _configuration(socket_path)
    metadata = configuration.get("rustc_sleep_emit_metadata")
    duration = configuration.get("rustc_emit_seconds")
    if isinstance(metadata, str) and isinstance(duration, (int, float)):
        environment["FAKE_RUSTC_SLEEP_EMIT_METADATA"] = metadata
        environment["FAKE_RUSTC_EMIT_SECONDS"] = str(duration)
    return environment


def _next_result(socket_path: str, state: ServerState) -> str:
    """Consume one scripted result, defaulting to a server-side cache miss.

    Returns
    -------
    str
        One accepted scripted result.
    """
    configuration = _configuration(socket_path)
    script = configuration.get("script", [])
    with _STATE_LOCK:
        index = state["script_index"]
        result = (
            script[index]
            if isinstance(script, list) and index < len(script)
            else "miss"
        )
        state["script_index"] += 1
        _save_state(socket_path, state)
    if isinstance(result, str) and result in {
        "hit",
        "miss",
        "not_cacheable",
        "not_compile",
    }:
        return result
    return "miss"


def _record_result(
    socket_path: str, state: ServerState, result: str, returncode: int
) -> None:
    """Advance and persist only the accepted request's counter footprint."""
    with _STATE_LOCK:
        counters = state["counters"]
        counters["compile_requests"] += 1
        match result:
            case "hit":
                counters["cache_hits"] += 1
                counters["requests_executed"] += 1
            case "not_cacheable":
                counters["requests_not_cacheable"] += 1
            case "not_compile":
                counters["requests_not_compile"] += 1
            case _:
                counters["compilations"] += 1
                counters["cache_misses"] += 1
                counters["requests_executed"] += 1
                counters["compile_fails"] += int(returncode != 0)
        _save_state(socket_path, state)


def _statistics_delay(socket_path: str, state: ServerState) -> float:
    """Count a query before its configured delay so tests observe in-flight work.

    Returns
    -------
    float
        The configured delay, or zero when no delay is scheduled.
    """
    return _statistics_delay_impl(
        socket_path,
        state,
        save_state=_save_state,
        configuration=_configuration,
    )


def client(argv: list[str], env: cabc.Mapping[str, str] | None = None) -> int:
    """Return fixture statistics or send one compile request to the server.

    Parameters
    ----------
    argv : list[str]
        The compiler's argument vector.
    env : Mapping[str, str] | None
        The environment naming optional detached-work fields.

    Returns
    -------
    int
        The statistics path returns zero; compile paths return the exit status.

    Examples
    --------
    ``client(["--show-stats", "--stats-format", "json"])`` writes the pinned
    fixture and returns zero.
    """
    if tuple(argv) == _STATISTICS_ARGUMENTS:
        return _request_statistics(os.environ if env is None else env)
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
