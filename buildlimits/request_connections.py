"""Unrecorded requester connections and peer-validation boundaries.

These helpers own only unrecorded sockets and frame descriptors. Requests owns
recorded Pending entries; admission alone owns compiler tokens. Existing helpers
are relocated here rather than introducing a second socket execution harness.
"""

import contextlib
import os
import selectors
import socket
import struct
import typing as typ

from buildlimits.descriptors import close_all
from buildlimits.protocol import (
    CompileRequest,
    ProtocolError,
    RecoverReport,
    Refused,
    Reply,
    StatusReport,
    decode_request,
    encode_reply,
    send_frame,
)
from buildlimits.request_validation import total_budget_refusal

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.pending_request import Pending

_UCRED = struct.Struct("3i")

# A supervisor that manages no cache cannot recover one, and answering with a
# fabricated phase would read as a live result.
_NO_RECOVERY = Refused(
    "no-cache-recovery", "this supervisor manages no sccache service"
)


def refuse_unrecorded(conn: socket.socket, fds: list[int], reply: Refused) -> None:
    """Refuse an unrecorded request and release its received resources.

    Parameters
    ----------
    conn : socket.socket
        Owned connection to close after attempting the refusal.
    fds : list[int]
        Owned received rights, cleared before they are closed.
    reply : Refused
        Bounded refusal sent before closing the connection.
    """
    _answer_once(conn, fds, lambda: reply)


def _release_rights(fds: list[int]) -> None:
    """Take ownership of received rights, clear the caller's list and close them.

    Clearing the list first means a failure while closing cannot leave the
    caller holding numbers it might close a second time.

    Parameters
    ----------
    fds : list[int]
        Received rights, emptied in place before they are closed.
    """
    owned, fds[:] = list(fds), []
    close_all(owned)


def _answer_once(
    conn: socket.socket, fds: list[int], reply: cabc.Callable[[], Reply]
) -> None:
    """Release received rights, then send one reply and close the connection.

    Every unrecorded answer follows this order: rights are released before
    the reply is built, so a failing provider cannot leak them, and the
    connection is closed even when the provider or the send fails.

    Parameters
    ----------
    conn : socket.socket
        Owned connection, always closed before this returns.
    fds : list[int]
        Owned received rights, cleared before they are closed.
    reply : Callable[[], Reply]
        Builds the reply once the rights are released; its failures propagate
        after the connection is closed.
    """
    _release_rights(fds)
    try:
        send_quietly(conn, reply())
    finally:
        close_quietly(conn)


@contextlib.contextmanager
def unrecorded(conn: socket.socket, fds: list[int]) -> cabc.Iterator[None]:
    """Reclaim unrecorded resources if dispatch fails before ownership transfers.

    Parameters
    ----------
    conn : socket.socket
        Connection whose normal ownership transfers or ends during dispatch.
    fds : list[int]
        Received rights, cleared before exceptional cleanup closes them.

    Yields
    ------
    None
        Dispatch scope; successful paths retain their existing cleanup owners.
    """
    try:
        yield
    except BaseException:
        _release_rights(fds)
        close_quietly(conn)
        raise


def listen(path: Path) -> socket.socket:
    """Create the supervisor's private listening socket.

    Parameters
    ----------
    path : Path
        Where to listen; the parent directory is made private to the owner.

    Returns
    -------
    socket.socket
        A non-blocking listener, readable and writable only by the owner.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen(128)
        listener.settimeout(0.0)
    except BaseException:
        close_quietly(listener)
        raise
    return listener


def check_peer(conn: socket.socket) -> None:
    """Accept connections only from the supervisor's own user.

    Parameters
    ----------
    conn : socket.socket
        A newly accepted connection.

    Raises
    ------
    ProtocolError
        If the peer runs as another user.
    """
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
    _pid, uid, _gid = _UCRED.unpack(raw)
    if uid != os.getuid():
        msg = f"connection from uid {uid} refused"
        raise ProtocolError(msg)


def send_quietly(conn: socket.socket, reply: Reply) -> None:
    """Send a reply on a connection not yet tracked, ignoring failures.

    Parameters
    ----------
    conn : socket.socket
        The connection.
    reply : Reply
        The reply.
    """
    with contextlib.suppress(OSError, ProtocolError):
        send_frame(conn, encode_reply(reply))


def close_quietly(conn: socket.socket) -> None:
    """Close a connection, ignoring errors.

    Parameters
    ----------
    conn : socket.socket
        The connection.
    """
    with contextlib.suppress(OSError):
        conn.close()


def reject_connection(
    conn: socket.socket,
    fds: list[int],
    error: BaseException,
    logger: cabc.Callable[[str], None],
) -> None:
    """End unrecorded receipt ownership before logging a bounded failure.

    Parameters
    ----------
    conn : socket.socket
        Unrecorded requester connection to close.
    fds : list[int]
        Received rights, cleared before their ownership ends.
    error : BaseException
        Receipt or protocol fault; only its type and errno are logged.
    logger : Callable[[str], None]
        Existing requester diagnostic sink; I/O failure cannot undo cleanup.
    """
    owned, fds[:] = list(fds), []
    close_all(owned)
    close_quietly(conn)
    with contextlib.suppress(OSError):
        logger(
            f"rejected connection: {type(error).__name__} "
            f"errno={getattr(error, 'errno', None)}"
        )


def answer_status(
    conn: socket.socket, fds: list[int], status: cabc.Callable[[], dict[str, object]]
) -> None:
    """Answer a status connection without retaining requester resources.

    Parameters
    ----------
    conn : socket.socket
        Unrecorded socket, always closed after producing the response.
    fds : list[int]
        Unexpected received rights, cleared and closed before querying status.
    status : Callable[[], dict[str, object]]
        Existing supervisor status provider; its failures propagate after cleanup.
    """
    _answer_once(conn, fds, lambda: StatusReport(status()))


def answer_recover(
    conn: socket.socket,
    fds: list[int],
    recover: cabc.Callable[[], dict[str, object]] | None,
) -> None:
    """Answer a recovery connection, refusing one this supervisor cannot serve.

    Parameters
    ----------
    conn : socket.socket
        Unrecorded socket, always closed after producing the response.
    fds : list[int]
        Unexpected received rights, cleared before they are closed.
    recover : Callable[[], dict[str, object]] | None
        Existing supervisor recovery provider, or ``None`` when no managed
        cache is configured; its failures propagate after cleanup. The
        supervisor binds the operator trigger and its clock, so this port
        stays a plain request with no arguments of its own.
    """
    if recover is None:
        refuse_unrecorded(conn, fds, _NO_RECOVERY)
        return
    provider = recover
    _answer_once(conn, fds, lambda: RecoverReport(provider()))


def truncation_refusal(body: bytes | None, budget: int) -> Refused:
    """Classify a truncated receipt from its complete declaration when available.

    Parameters
    ----------
    body : bytes | None
        Complete bounded frame body, or None after an incomplete receipt.
    budget : int
        Known total request retention ceiling; zero permits an idle recheck.

    Returns
    -------
    Refused
        A final descriptor refusal for a proven total-budget excess; otherwise
        a retryable busy refusal. Partial rights never prove permanent excess.
    """
    busy = Refused(
        "busy", "descriptor receipt exhausted receiver headroom; retry admission"
    )
    if body is None:
        return busy
    try:
        request = decode_request(body)
    except ProtocolError:
        return busy
    if not isinstance(request, CompileRequest):
        return busy
    refusal = total_budget_refusal(len(request.fd_targets), budget)
    return busy if refusal is None else Refused(refusal.reason, refusal.message)


def record_pending(
    selector: selectors.BaseSelector,
    pending: dict[int, Pending],
    entry: Pending,
    observe: cabc.Callable[[], None],
) -> None:
    """Register a recorded connection atomically with its ownership table.

    Parameters
    ----------
    selector : selectors.BaseSelector
        Request owner selector to update.
    pending : dict[int, Pending]
        Request owner table to update transactionally.
    entry : Pending
        Request whose connection must still be owned.
    observe : Callable[[], None]
        Samples retained resources after table insertion.

    Raises
    ------
    ValueError
        If the entry already has no connection to register.
    """
    if entry.conn is None:
        msg = "cannot record a disconnected request"
        raise ValueError(msg)
    try:
        entry.conn.settimeout(0.0)
        selector.register(entry.conn, selectors.EVENT_READ, ("conn", entry.request_id))
        pending[entry.request_id] = entry
        observe()
    except BaseException:
        if pending.get(entry.request_id) is entry:
            pending.pop(entry.request_id)
        if entry.conn is not None:
            with contextlib.suppress(OSError, ValueError, KeyError):
                key = selector.get_key(entry.conn)
                if key.data == ("conn", entry.request_id):
                    selector.unregister(entry.conn)
        raise


def send_recorded(entry: Pending, reply: Reply, timeout: float) -> bool:
    """Attempt a recorded reply without taking connection-cleanup ownership.

    Parameters
    ----------
    entry : Pending
        Request owning the connection, or an already disconnected request.
    reply : Reply
        Existing protocol reply to send.
    timeout : float
        Existing bounded send timeout.

    Returns
    -------
    bool
        False after an I/O or protocol failure; disconnected entries need no work.
    """
    if entry.conn is None:
        return True
    try:
        entry.conn.settimeout(timeout)
        send_frame(entry.conn, encode_reply(reply))
        entry.conn.settimeout(0.0)
    except OSError, ProtocolError:
        return False
    return True


def drop_recorded(selector: selectors.BaseSelector, entry: Pending) -> None:
    """Release a recorded connection while retaining its request metadata.

    Parameters
    ----------
    selector : selectors.BaseSelector
        Selector which may still watch the connection.
    entry : Pending
        Request whose connection reference is cleared before closing.
    """
    if entry.conn is None:
        return
    with contextlib.suppress(OSError, KeyError, ValueError):
        selector.unregister(entry.conn)
    conn, entry.conn = entry.conn, None
    close_quietly(conn)
