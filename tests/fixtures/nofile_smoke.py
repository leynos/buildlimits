"""Stress queued descriptor retention in a disposable admission supervisor.

The Molecule scenario runs this against its provisioned unit. Requests use
real protocol frames and SCM_RIGHTS, while short Python programs stand in for
compilers. This checks capacity mitigation, not leak freedom.
"""

import contextlib
import dataclasses
import json
import os
import socket
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from buildlimits import attributes, descriptors, protocol
from buildlimits.status_client import query

REQUESTS = 160
SLOTS = 2
OLD_NOFILE_LIMIT = 1024


def _wait_for_pressure(pid: int, socket_path: Path) -> int:
    """Observe queued requests and descriptor pressure on the real process.

    Returns
    -------
    int
        The descriptor count once the queue exceeds the old ceiling.

    Raises
    ------
    RuntimeError
        If the supervisor is unhealthy or the pressure deadline expires.
    """
    count = 0
    status: dict[str, object] = {}
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = query(env={"BUILD_LIMITS_SOCKET": str(socket_path)})
        count = len(list(Path(f"/proc/{pid}/fd").iterdir()))
        if not status["healthy"]:
            msg = f"unhealthy supervisor: {status}"
            raise RuntimeError(msg)
        if _has_pressure(status, count):
            return count
        time.sleep(0.05)
    msg = f"did not retain >1024 descriptors: count={count}, status={status}"
    raise RuntimeError(msg)


def _has_pressure(status: dict[str, object], count: int) -> bool:
    """Require the full waiting queue, held slots and measured headroom.

    Returns
    -------
    bool
        Whether every pressure precondition is observed.
    """
    queued = status["queued"]
    return (
        _has_full_queue(queued) and status["held"] == SLOTS and count > OLD_NOFILE_LIMIT
    )


def _has_full_queue(queued: object) -> bool:
    """Return whether all requests beyond the available slots are queued.

    Returns
    -------
    bool
        Whether the waiting list contains every unadmitted request.
    """
    return isinstance(queued, list) and len(queued) == REQUESTS - SLOTS


def _require_success(connection: socket.socket) -> None:
    """Read progress replies until the compiler successfully finishes.

    Raises
    ------
    RuntimeError
        If a request is refused, fails, or closes without a finished reply.
    """
    while (frame := protocol.recv_frame(connection)) is not None:
        body, fds = frame
        descriptors.close_all(fds)
        reply = protocol.decode_reply(body)
        match reply:
            case protocol.Refused(message=message):
                raise RuntimeError(message)
            case protocol.Finished(exit=outcome):
                if outcome.code != 0 or outcome.signal is not None:
                    msg = f"compiler failed: {outcome}"
                    raise RuntimeError(msg)
                return
    msg = "supervisor closed a request without a finished reply"
    raise RuntimeError(msg)


def _child_script(release: Path, nice: int) -> str:
    """Build the child program that verifies its post-exec caller attributes.

    Returns
    -------
    str
        The standalone child program with its release gate and assertions.
    """
    # Every child waits until all requests are queued. Resource assertions
    # run after exec, so they cannot accidentally inspect the test driver.
    return (
        "import os, resource, time; from pathlib import Path; "
        f"gate=Path({str(release)!r}); "
        "deadline=time.monotonic()+60; "
        "\nwhile not gate.exists() and time.monotonic()<deadline: "
        "time.sleep(0.02)\n"
        "assert gate.exists(); "
        "assert resource.getrlimit(resource.RLIMIT_NOFILE)==(1024,4096); "
        "assert os.umask(0)==0o027; "
        f"assert os.getpriority(os.PRIO_PROCESS,0)=={nice}; "
        "[os.fstat(fd) for fd in range(7)]"
    )


def _submit_requests(
    stack: contextlib.ExitStack,
    socket_path: Path,
    request: protocol.CompileRequest,
    source_fd: int,
) -> list[socket.socket]:
    """Submit bounded batches while the stack owns every partial connection.

    Returns
    -------
    list[socket.socket]
        Connected clients whose replies must be drained concurrently.
    """
    connections: list[socket.socket] = []
    for index in range(REQUESTS):
        connection = stack.enter_context(
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        )
        connection.settimeout(120)
        connection.connect(str(socket_path))
        labelled = dataclasses.replace(request, label=f"nofile-smoke-{index}")
        protocol.send_frame(
            connection, protocol.encode_request(labelled), [source_fd] * 7
        )
        connections.append(connection)
        if (index + 1) % 32 == 0:
            # Let the selector accept each batch before its backlog fills.
            query(env={"BUILD_LIMITS_SOCKET": str(socket_path)})
    return connections


def smoke(socket_path: Path, pid: int) -> dict[str, int]:
    """Queue concurrent requests above the old ceiling and verify every launch.

    Parameters
    ----------
    socket_path : Path
        The disposable supervisor's protocol socket.
    pid : int
        Its MainPID, used to observe retained descriptors.

    Returns
    -------
    dict[str, int]
        Number of completed requests and observed supervisor descriptors.

    Notes
    -----
    Pressure and launch failures propagate from the protocol checks.
    The supervisor must be disposable and idle, with two slots. The fixture
    sends requested caller attributes without altering the driver's own limits.

    Examples
    --------
    Run against the Molecule fixture's provisioned supervisor:

    >>> address = Path('/run/user/1000/buildlimits/supervisor.sock')
    >>> result = smoke(address, 1234)  # doctest: +SKIP
    >>> result['completed']  # doctest: +SKIP
    160
    """
    caller = dataclasses.replace(
        attributes.capture(),
        umask=0o027,
        nice=os.getpriority(os.PRIO_PROCESS, 0) + 1,
        rlimits={"RLIMIT_NOFILE": (1024, 4096)},
    )
    with tempfile.TemporaryDirectory(prefix="nofile-smoke-") as directory:
        work = Path(directory)
        release = work / "release"
        with contextlib.ExitStack() as stack:
            null = stack.enter_context(Path(os.devnull).open("r+b", buffering=0))
            try:
                script = _child_script(release, caller.nice)
                request = protocol.CompileRequest(
                    argv=(sys.executable, "-c", script),
                    cwd=directory,
                    env=dict(os.environ),
                    attributes=caller,
                    fd_targets=tuple(range(7)),
                    label="nofile-smoke",
                    cacheable=False,
                )
                connections = _submit_requests(
                    stack, socket_path, request, null.fileno()
                )
                count = _wait_for_pressure(pid, socket_path)
            finally:
                # Release admitted children even when setup or pressure fails.
                release.touch()
            # Real adapters drain their own replies concurrently. Leaving later
            # clients unread fills progress-response buffers and can stall the
            # supervisor, creating backpressure unrelated to descriptor capacity.
            with ThreadPoolExecutor(max_workers=REQUESTS) as readers:
                list(readers.map(_require_success, connections))
    return {"completed": REQUESTS, "supervisor_descriptors": count}


if __name__ == "__main__":
    print(json.dumps(smoke(Path(sys.argv[1]), int(sys.argv[2]))))
