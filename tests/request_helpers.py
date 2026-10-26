"""Shared requests for supervisor, descriptor-pressure and ownership regressions."""

import contextlib
import os
import selectors
import socket
import typing as typ

from buildlimits.attributes import AttributePlan, ProcessAttributes
from buildlimits.pending_request import Pending
from buildlimits.protocol import CompileRequest, Refused, decode_reply, recv_frame
from buildlimits.requesters import Requests

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def compile_request(targets: tuple[int, ...] = ()) -> CompileRequest:
    """Construct a direct compiler request for descriptor-ownership tests.

    Parameters
    ----------
    targets : tuple[int, ...]
        Caller descriptor numbers to recreate in a disposable launch child.

    Returns
    -------
    CompileRequest
        A request for /bin/true with current niceness and no resource changes.
    """
    return CompileRequest(
        ("/bin/true",),
        "/",
        {},
        ProcessAttributes(0o22, os.getpriority(os.PRIO_PROCESS, 0), {}),
        targets,
        "pressure",
        cacheable=False,
    )


def crate_pending(
    request_id: int, group_id: str | None = None, *, admitted: float | None = None
) -> Pending:
    """Record one disconnected ``rustc -vV`` request that owns no descriptors.

    Parameters
    ----------
    request_id : int
        Identifier of the recorded request.
    group_id : str or None
        The compiler group it was admitted into, or None while it waits.
    admitted : float or None
        When it was admitted, or None while it waits.

    Returns
    -------
    Pending
        A request with neutral attributes, arrived at time zero.
    """
    request = CompileRequest(
        argv=("rustc", "-vV"),
        cwd="/srv/crate",
        env={},
        attributes=ProcessAttributes(umask=0o22, nice=0, rlimits={}),
        fd_targets=(),
        label="crate",
    )
    return Pending(
        request_id,
        None,
        request,
        [],
        AttributePlan(umask=0o22, nice=0, rlimits={}),
        arrived=0.0,
        group_id=group_id,
        admitted=admitted,
    )


@contextlib.contextmanager
def recorded_request() -> cabc.Generator[
    tuple[selectors.BaseSelector, socket.socket, Requests, Pending]
]:
    """Record one validated direct request over a real connection.

    The request is forgotten when the block ends, so its received resources
    are released even when an assertion inside the block fails.

    Yields
    ------
    tuple[selectors.BaseSelector, socket.socket, Requests, Pending]
        The selector, the requester's end of the connection, the requester
        table and the recorded entry.

    Raises
    ------
    AssertionError
        If the requester table refuses to record the request.
    """
    client, server = socket.socketpair()
    with selectors.DefaultSelector() as selector, client, server:
        requests = Requests(selector, clock=lambda: 0.0)
        entry = requests._admit(server, compile_request(), [])
        if entry is None:
            msg = "a validated request must be recorded"
            raise AssertionError(msg)
        try:
            yield selector, client, requests, entry
        finally:
            requests.forget(entry.request_id)


def refusal_from(client: socket.socket) -> Refused:
    """Read the one reply a refused requester receives.

    Parameters
    ----------
    client : socket.socket
        The requester's end of the connection.

    Returns
    -------
    Refused
        The decoded refusal, for the test to check its reason and message.

    Raises
    ------
    AssertionError
        If the supervisor closed the connection without replying.
    TypeError
        If the reply is anything but a refusal.
    """
    frame = recv_frame(client)
    if frame is None:
        msg = "the requester must receive a reply"
        raise AssertionError(msg)
    reply = decode_reply(frame[0])
    if not isinstance(reply, Refused):
        msg = f"the requester must be refused, got {reply!r}"
        raise TypeError(msg)
    return reply
