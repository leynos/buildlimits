"""Connection ownership and bounded supervisor-availability retries.

Only adapter admission attempts use this boundary. It never executes compilers
or changes admission controls; queued progress remains in the adapter.
"""

import contextlib
import dataclasses as dc
import socket
import time
import typing as typ

from buildlimits.heartbeat import Heartbeat, write_line

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


class AdapterError(Exception):
    """The adapter cannot run the compiler."""


def unix_socket() -> socket.socket:
    """Create an unconnected Unix stream socket.

    Returns
    -------
    socket.socket
        The socket, owned by the caller.
    """
    return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)


@dc.dataclass(frozen=True, slots=True)
class Dialler:
    """How the adapter reaches the supervisor.

    Attributes
    ----------
    make_socket : Callable[[], socket.socket]
        Creates each connection attempt's socket.
    sleep : Callable[[float], None]
        Waits between attempts.
    attempted : Callable[[], None] | None
        Counts each physical connection attempt without retaining socket details.
    """

    make_socket: cabc.Callable[[], socket.socket] = unix_socket
    sleep: cabc.Callable[[float], None] = time.sleep
    attempted: cabc.Callable[[], None] | None = None

    def _record_attempt(self) -> None:
        """Notify an optional observer before allocating the attempt's socket."""
        if self.attempted is not None:
            self.attempted()


SYSTEM_DIALLER = Dialler()


def connect(
    path: Path, deadline: float, heartbeat: Heartbeat, dialler: Dialler = SYSTEM_DIALLER
) -> socket.socket:
    """Connect to the supervisor, waiting while it is unavailable.

    Parameters
    ----------
    path : Path
        The supervisor's socket.
    deadline : float
        The monotonic time after which to give up.
    heartbeat : Heartbeat
        Reports the wait and supplies its clock.
    dialler : Dialler
        Creates sockets and waits between attempts.

    Returns
    -------
    socket.socket
        The connection.

    Raises
    ------
    AdapterError
        If the supervisor stays unavailable until the deadline.
    """
    while True:
        dialler._record_attempt()
        conn = None
        try:
            conn = dialler.make_socket()
            conn.settimeout(max(0.001, deadline - heartbeat.clock()))
            conn.connect(str(path))
            conn.settimeout(None)
        except BaseException as error:
            if conn is not None:
                with contextlib.suppress(OSError):
                    conn.close()
            if not isinstance(error, OSError):
                raise
            remaining = deadline - heartbeat.clock()
            if remaining <= 0:
                msg = f"the build-limits supervisor is unavailable at {path}: {error}"
                raise AdapterError(msg) from error
            write_line(heartbeat.tick(waiting_for="the build-limits supervisor"))
            dialler.sleep(min(remaining, 1.0, max(0.05, heartbeat.timeout())))
            continue
        return conn
