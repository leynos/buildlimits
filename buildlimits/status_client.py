"""Fetch the supervisor's status document over its socket.

The transport and the socket's location are parameters, so the query can be
exercised against any connected stream; by default it connects to the Unix
socket that :func:`buildlimits.config.supervisor_socket` names for this
user. Every way the query can fail is reported as :class:`StatusQueryError`.
"""

import os
import socket
import typing as typ

from buildlimits import config, descriptors
from buildlimits.protocol import (
    ProtocolError,
    StatusReport,
    StatusRequest,
    decode_reply,
    encode_request,
    recv_frame,
    send_frame,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.protocol import Reply, Request


class StatusQueryError(Exception):
    """The supervisor could not be asked for its status, or did not answer."""


def connect_unix(path: Path, timeout: float) -> socket.socket:
    """Connect to a Unix stream socket.

    Parameters
    ----------
    path : Path
        The socket's path.
    timeout : float
        Seconds to allow for connecting and for each later read or write.

    Returns
    -------
    socket.socket
        The connected socket, owned by the caller.

    Raises
    ------
    OSError
        If the socket cannot be connected; it is closed first.
    """
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(timeout)
        conn.connect(str(path))
    except OSError:
        conn.close()
        raise
    return conn


CLOSED_WITHOUT_REPLY = "the supervisor closed the connection without replying"


def exchange(conn: socket.socket, request: Request) -> Reply | None:
    """Send one request over an owned connection and decode its single reply.

    The status and recovery clients share this round trip; each translates
    its failures into its own error type.

    Parameters
    ----------
    conn : socket.socket
        A connected stream, owned by this call and closed before it returns.
    request : Request
        The request to send.

    Returns
    -------
    Reply | None
        The decoded reply, or ``None`` when the supervisor closed the
        connection without replying. Any descriptors passed with the reply
        are closed, because no client request expects them.

    Raises
    ------
    OSError
        If the request cannot be sent or the reply cannot be read.
    ProtocolError
        If the reply is not a valid frame.

    Examples
    --------
    A client connects first, so a refused connection never reaches here::

        reply = exchange(connect_unix(path, 5.0), StatusRequest())
    """  # ruff: ignore[docstring-extraneous-exception] - the framing calls raise them.
    with conn:
        send_frame(conn, encode_request(request))
        frame = recv_frame(conn)
    if frame is None:
        return None
    body, fds = frame
    descriptors.close_all(fds)
    return decode_reply(body)


def query(
    timeout: float = 5.0,
    env: cabc.Mapping[str, str] = os.environ,
    connect: cabc.Callable[[Path, float], socket.socket] = connect_unix,
) -> dict[str, object]:
    """Ask the supervisor for its status.

    Parameters
    ----------
    timeout : float
        Seconds to wait for the connection and the reply.
    env : Mapping[str, str]
        The environment that locates the supervisor's socket.
    connect : Callable[[Path, float], socket.socket]
        Opens a connected stream to the socket.

    Returns
    -------
    dict[str, object]
        The status document.

    Raises
    ------
    StatusQueryError
        If the supervisor cannot be reached, closes the connection without
        replying, or replies with anything but a status report.
    """
    path = config.supervisor_socket(env)
    try:
        reply = exchange(connect(path, timeout), StatusRequest())
    except (OSError, ProtocolError) as error:
        msg = f"cannot query the supervisor at {path}: {error}"
        raise StatusQueryError(msg) from error
    if reply is None:
        raise StatusQueryError(CLOSED_WITHOUT_REPLY)
    if not isinstance(reply, StatusReport):
        msg = f"the supervisor sent {reply!r} instead of its status"
        raise StatusQueryError(msg)
    return reply.state
