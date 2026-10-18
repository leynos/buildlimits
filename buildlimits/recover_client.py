"""Ask the supervisor to recover the managed cache's retained work.

The transport and the socket's location are parameters, so the request can be
exercised against any connected stream; by default it connects to the Unix
socket that :func:`buildlimits.config.supervisor_socket` names for this
user. Every way the request can fail is reported as :class:`RecoverQueryError`.

The supervisor answers as soon as the transaction is open, so a successful
reply proves only that recovery has begun. Callers that need the outcome poll
the status document, which reports the transaction's phase.
"""

import os
import typing as typ

from buildlimits import config
from buildlimits.protocol import ProtocolError, RecoverReport, RecoverRequest, Refused
from buildlimits.status_client import (
    CLOSED_WITHOUT_REPLY,
    StatusQueryError,
    connect_unix,
    exchange,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import socket
    from pathlib import Path


class RecoverQueryError(Exception):
    """The supervisor could not be asked to recover, or did not answer."""


def request(
    timeout: float = 5.0,
    env: cabc.Mapping[str, str] = os.environ,
    connect: cabc.Callable[[Path, float], socket.socket] = connect_unix,
) -> dict[str, object]:
    """Ask the supervisor to open one cache-recovery transaction.

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
        The transaction's phase, the consecutive failures of its last action,
        the last fault text, the seconds until that action is retried, and
        ``blocked``: empty while the transaction runs, otherwise why it must
        not start.

    Raises
    ------
    RecoverQueryError
        If the supervisor cannot be reached, closes the connection without
        replying, refuses the request, or replies with anything but a recovery
        report.
    """
    path = config.supervisor_socket(env)
    try:
        reply = exchange(connect(path, timeout), RecoverRequest())
    except (OSError, ProtocolError) as error:
        msg = f"cannot ask the supervisor at {path} to recover: {error}"
        raise RecoverQueryError(msg) from error
    if reply is None:
        raise RecoverQueryError(CLOSED_WITHOUT_REPLY)
    # A recovery request is answered only by a report: a refusal or any other
    # reply means the supervisor is not running the transaction that was asked
    # for, so the caller is told rather than handed a stand-in document.
    match reply:
        case RecoverReport(state=state):
            return state
        case Refused(message=message):
            msg = f"the supervisor refused the recovery: {message}"
        case _:
            msg = f"the supervisor sent {reply!r} instead of a recovery report"
    raise RecoverQueryError(msg)


__all__ = ["RecoverQueryError", "StatusQueryError", "connect_unix", "request"]
