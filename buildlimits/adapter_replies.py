"""Waiting for the supervisor's next reply, with heartbeats between replies.

Only the compiler adapter uses this boundary. It reads one framed reply, writes a
heartbeat line when the interval passes first, and reports a supervisor that
closes or resets the connection as :class:`SupervisorClosedError`, which the
caller reads as a restart or as a lost compiler according to what it was told
before.
"""

import os
import select
import typing as typ

from buildlimits.adapter_connection import AdapterError, SupervisorClosedError
from buildlimits.framing import TruncatedFrameError
from buildlimits.heartbeat import write_line
from buildlimits.protocol import decode_reply, recv_frame

if typ.TYPE_CHECKING:
    import socket

    from buildlimits.heartbeat import Heartbeat
    from buildlimits.protocol import Reply


def next_reply(
    conn: socket.socket, heartbeat: Heartbeat, *, deadline: float | None = None
) -> Reply | None:
    """Wait for one reply, writing a heartbeat line if one falls due.

    Returns
    -------
    Reply | None
        The reply, or ``None`` when the heartbeat interval passed first.

    Raises
    ------
    AdapterError
        If the supervisor closes the connection.
    """
    timeout = heartbeat.timeout()
    if deadline is not None:
        remaining = deadline - heartbeat.clock()
        if remaining <= 0:
            msg = (
                "compile admission did not acknowledge the request before its deadline"
            )
            raise AdapterError(msg)
        timeout = min(timeout, remaining)
    ready, _writable, _errored = select.select([conn], [], [], timeout)
    if not ready:
        write_line(heartbeat.tick())
        return None
    return receive_reply(conn, heartbeat, deadline)


def receive_reply(
    conn: socket.socket, heartbeat: Heartbeat, deadline: float | None
) -> Reply:
    """Decode a ready reply, reclaim stray fds and always restore its timeout.

    Returns
    -------
    Reply
        Decoded reply after reclaiming unsolicited descriptors.

    Raises
    ------
    AdapterError
        If the reply deadline expires.
    SupervisorClosedError
        If the supervisor closes or resets the connection.
    """
    original = conn.gettimeout()
    try:
        if deadline is not None:
            remaining = deadline - heartbeat.clock()
            if remaining <= 0:
                msg = "compile admission reply deadline expired"
                raise AdapterError(msg)
            conn.settimeout(remaining)
        frame = recv_frame(conn)
    except ConnectionError, TruncatedFrameError:
        frame = None
    finally:
        conn.settimeout(original)
    if frame is None:
        msg = "the build-limits supervisor closed the connection"
        raise SupervisorClosedError(msg)
    body, stray = frame
    for fd in stray:
        os.close(fd)
    return decode_reply(body)
