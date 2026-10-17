"""Recorded requester state; resource ownership remains with Requests.

The record carries the existing request and launch metadata across admission
effects. It introduces no independent cleanup or token-accounting authority.
"""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import socket

    from buildlimits import attributes
    from buildlimits.protocol import CompileRequest


@dc.dataclass(slots=True)
class Pending:
    """A request the supervisor has accepted, from arrival to result.

    Attributes
    ----------
    request_id : int
        The admission identifier.
    conn : socket.socket | None
        The requester's connection, until it closes.
    request : CompileRequest
        What to run.
    fds : list[int]
        The requester's descriptors, until the launch child has them.
    plan : attributes.AttributePlan
        The attributes the compiler will receive.
    arrived : float
        When the request arrived, on the monotonic clock.
    group_id : str | None
        The group serving the request, once admitted.
    admitted : float | None
        When it was admitted.
    exec_error : str | None
        Why the compiler could not start, if it could not.
    cancellation : str
        The bounded first cancellation cause, or ``none``.
    cache_path : str
        The composition selected at successful admission.
    """

    request_id: int
    conn: socket.socket | None
    request: CompileRequest
    fds: list[int]
    plan: attributes.AttributePlan
    arrived: float
    group_id: str | None = None
    admitted: float | None = None
    exec_error: str | None = None
    cancellation: str = "none"
    cache_path: str = "direct"
