"""Frames: the length-prefixed, versioned JSON units of the wire protocol.

A frame is a four-byte big-endian length followed by that many bytes of
UTF-8 JSON. Descriptors travel as ``SCM_RIGHTS`` ancillary data on the same
``sendmsg`` call as the frame that names them.
"""

import array
import contextlib
import dataclasses as dc
import errno
import resource
import socket
import time
import typing as typ

from buildlimits.descriptors import close_all
from buildlimits.frame_payload import _LENGTH, _frame_length
from buildlimits.frame_payload import MAX_FRAME_BYTES as MAX_FRAME_BYTES
from buildlimits.frame_payload import PROTOCOL_VERSION as PROTOCOL_VERSION
from buildlimits.frame_payload import ProtocolError as ProtocolError
from buildlimits.frame_payload import decode_body as decode_body
from buildlimits.frame_payload import encode_frame as encode_frame

if typ.TYPE_CHECKING:
    import collections.abc as cabc

MAX_FDS = 253  # SCM_MAX_FD on Linux.


@dc.dataclass(slots=True)
class _FrameReceipt:
    """Own one frame's rights and truncation state across header and body."""

    deadline: float | None
    max_fds: int
    clock: cabc.Callable[[], float] = time.monotonic
    fds: list[int] = dc.field(default_factory=list)
    truncated: bool = False

    def discard_rights(self) -> None:
        """Close owned rights before the next potentially blocking receive."""
        close_all(self.fds)
        self.fds.clear()

    def collect(self, rights: list[int], *, truncated: bool) -> None:
        """Transfer chunk ownership, discarding all rights after truncation."""
        self.fds.extend(rights)
        self.truncated = self.truncated or truncated
        if self.truncated:
            self.discard_rights()


class DescriptorTruncationError(ProtocolError):
    """Report lost rights without including potentially sensitive frame bytes.

    Parameters
    ----------
    body : bytes | None
        Complete frame body when available; never part of the error message.
        An exact-reader error outside a complete frame has no body.
    """

    def __init__(self, body: bytes | None = None) -> None:
        """Keep complete frame bytes separate from exception display and args."""
        super().__init__(
            "descriptor receipt truncated; "
            "receiver headroom or ancillary limit exhausted"
        )
        self.body = body


class DescriptorSendPressureError(ProtocolError):
    """The initial rights transfer was refused before any request bytes queued."""


def _send_initial(sock: socket.socket, frame: bytes, fds: cabc.Sequence[int]) -> int:
    """Transfer the first bytes, distinguishing untransferred rights pressure.

    Returns
    -------
    int
        Bytes transferred by the initial send.

    """
    if not fds:
        return sock.send(frame)
    return _send_rights(sock, frame, fds)


def _send_rights(sock: socket.socket, frame: bytes, fds: cabc.Sequence[int]) -> int:
    """Transfer rights once, distinguishing pressure before any frame bytes queue.

    Returns
    -------
    int
        Number of initial frame bytes transferred.

    Raises
    ------
    DescriptorSendPressureError
        If Linux rejects the initial in-flight rights transfer.
    OSError
        If transfer fails for any other reason.
    """
    try:
        sent = socket.send_fds(sock, [frame], list(fds))
    except OSError as error:
        if error.errno != errno.ETOOMANYREFS:
            raise
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        msg = (
            f"stage=send-rights errno={error.errno} nofile={soft}:{hard} "
            f"rights={len(fds)}; no request transferred"
        )
        raise DescriptorSendPressureError(msg) from error
    return sent


def send_frame(sock: socket.socket, frame: bytes, fds: cabc.Sequence[int] = ()) -> None:
    """Send one frame, with optional descriptors attached to its first bytes.

    Parameters
    ----------
    sock : socket.socket
        A connected Unix stream socket.
    frame : bytes
        The encoded frame.
    fds : Sequence[int]
        Descriptors to pass. At most :data:`MAX_FDS`.

    Raises
    ------
    ProtocolError
        If too many descriptors are passed.

    Notes
    -----
    The initial ancillary send can propagate ``DescriptorSendPressureError``
    without transferring bytes. Later write errors remain final.
    """
    if len(fds) > MAX_FDS:
        msg = f"cannot pass {len(fds)} descriptors; the limit is {MAX_FDS}"
        raise ProtocolError(msg)
    sent = _send_initial(sock, frame, fds)
    if sent < len(frame):
        sock.sendall(frame[sent:])


def recv_frame(
    sock: socket.socket,
    *,
    max_fds: int = MAX_FDS,
    clock: cabc.Callable[[], float] = time.monotonic,
) -> tuple[bytes, list[int]] | None:
    """Receive one frame and any descriptors sent with it.

    Parameters
    ----------
    sock : socket.socket
        A connected Unix stream socket.
    max_fds : int
        Maximum rights accepted across the whole frame, bounded by MAX_FDS.
    clock : Callable[[], float]
        Monotonic clock, in seconds, that sets and checks the total receive
        deadline; injectable so tests need not patch module-global time.

    Returns
    -------
    tuple[bytes, list[int]] | None
        The frame body and received descriptors, or ``None`` at end of stream
        before any byte of a new frame.

    Raises
    ------
    ProtocolError
        If the stream ends inside a frame or a frame is too large.
    DescriptorTruncationError
        If rights were lost. Only a complete, bounded frame supplies its body
        on the exception; all received rights are closed before waiting further.
    ValueError
        If the receive ceiling is outside the supported descriptor range.
    """  # ruff: ignore[docstring-extraneous-exception] - frame validators raise ProtocolError.
    if not 0 <= max_fds <= MAX_FDS:
        msg = "descriptor receive ceiling is outside the protocol limit"
        raise ValueError(msg)
    timeout = sock.gettimeout()
    # One total deadline spans header and body of a blocking frame.
    has_deadline = timeout is not None and timeout > 0
    deadline = clock() + timeout if has_deadline else None
    receipt = _FrameReceipt(deadline, max_fds, clock)
    try:
        header, _ = _recv_exact_with_fds(sock, _LENGTH.size, _receipt=receipt)
        if header is None:
            receipt.discard_rights()
            return None
        length = _frame_length(header)
        body, _ = _recv_exact_with_fds(sock, length, _receipt=receipt)
        body = _require_body(body)
    except BaseException:
        receipt.discard_rights()
        raise
    else:
        if receipt.truncated:
            raise DescriptorTruncationError(body)
        return body, receipt.fds
    finally:
        with contextlib.suppress(OSError):
            sock.settimeout(timeout)


def _recv_exact_with_fds(
    sock: socket.socket,
    size: int,
    *,
    max_fds: int = MAX_FDS,
    _receipt: _FrameReceipt | None = None,
) -> tuple[bytes | None, list[int]]:
    """Read exactly ``size`` bytes, collecting any descriptors on the way.

    Parameters
    ----------
    sock : socket.socket
        The connection to read.
    size : int
        The number of bytes to read.
    max_fds : int
        Rights ceiling for a standalone read.
    _receipt : _FrameReceipt | None
        Shared frame ownership and deadline, or a standalone exact receipt.

    Returns
    -------
    tuple[bytes | None, list[int]]
        The bytes, or ``None`` at end of stream before the first byte, and
        every descriptor received meanwhile.

    Raises
    ------
    DescriptorTruncationError
        If a standalone exact read lost rights; complete frame classification
        belongs to the caller sharing receipt state across header and body.
    """
    receipt = _receipt if _receipt is not None else _FrameReceipt(None, max_fds)
    try:
        data = _receive_bytes(sock, size, receipt)
        complete = _complete_receipt(data, size)
    except BaseException:
        receipt.discard_rights()
        raise
    if _receipt is None and receipt.truncated:
        raise DescriptorTruncationError
    return complete, receipt.fds


def _receive_bytes(
    sock: socket.socket,
    size: int,
    receipt: _FrameReceipt,
) -> bytes:
    """Accumulate chunks while transferring each installed right to the owner.

    Returns
    -------
    bytes
        Received bytes up to the requested count or EOF.
    """
    chunks: list[bytes] = []
    received = 0
    while received < size:
        if receipt.deadline is not None:
            sock.settimeout(_remaining_seconds(receipt.deadline, receipt.clock))
        ceiling = 0 if receipt.truncated else receipt.max_fds - len(receipt.fds)
        data, new_fds, truncated = _receive_chunk(sock, size - received, ceiling)
        receipt.collect(new_fds, truncated=truncated)
        if not data:
            break
        chunks.append(data)
        received += len(data)
    return b"".join(chunks)


def _complete_receipt(data: bytes, size: int) -> bytes | None:
    """Distinguish boundary EOF from an incomplete frame.

    Returns
    -------
    bytes | None
        Complete bytes, or EOF before any requested byte.

    Raises
    ------
    ProtocolError
        If EOF interrupts a nonempty partially received frame.
    """
    if not data and size > 0:
        return None
    if len(data) < size:
        msg = "stream ended inside a frame"
        raise ProtocolError(msg)
    return data


def _remaining_seconds(deadline: float, clock: cabc.Callable[[], float]) -> float:
    """Require positive time remaining within the shared frame deadline.

    Parameters
    ----------
    deadline : float
        Absolute deadline on ``clock``'s timeline.
    clock : Callable[[], float]
        Monotonic clock that measures the remaining time.

    Returns
    -------
    float
        Remaining socket timeout in seconds.

    Raises
    ------
    TimeoutError
        If the frame's total deadline has expired.
    """
    remaining = deadline - clock()
    if remaining <= 0:
        msg = "frame receipt deadline expired"
        raise TimeoutError(msg)
    return remaining


def _received_rights(ancillary: list[tuple[int, int, bytes]]) -> list[int]:
    """Decode every complete right installed by the kernel.

    Returns
    -------
    list[int]
        Installed rights owned by the receiving chunk.
    """
    fds: list[int] = []
    for level, kind, value in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            fds.extend(_decode_rights(value))
    return fds


def _receive_chunk(
    sock: socket.socket, size: int, max_fds: int
) -> tuple[bytes, list[int], bool]:
    """Receive one Linux chunk within an exact ancillary descriptor ceiling.

    Returns
    -------
    tuple[bytes, list[int], bool]
        Data, owned rights and whether ancillary receipt was truncated.
    """
    # Linux CMSG_LEN excludes padding that could admit an additional right
    # beyond an odd ceiling; raw recvmsg also applies atomic CLOEXEC flags.
    ancillary_size = (
        socket.CMSG_LEN(max_fds * array.array("i").itemsize) if max_fds else 0
    )
    data, ancillary, flags, _addr = sock.recvmsg(
        size, ancillary_size, socket.MSG_CMSG_CLOEXEC
    )
    fds = _received_rights(ancillary)
    return data, fds, bool(flags & socket.MSG_CTRUNC or len(fds) > max_fds)


def _require_body(body: bytes | None) -> bytes:
    """Require a body after an already received frame header.

    Returns
    -------
    bytes
        The received body, including a valid empty body.

    Raises
    ------
    ProtocolError
        If the stream ended before the body began.
    """
    if body is None:
        msg = "stream ended inside a frame"
        raise ProtocolError(msg)
    return body


def _decode_rights(value: bytes) -> list[int]:
    """Decode complete integer rights while ignoring ancillary padding bytes.

    Returns
    -------
    list[int]
        Descriptors actually encoded in the kernel's ancillary value.
    """
    rights = array.array("i")
    rights.frombytes(value[: len(value) - len(value) % rights.itemsize])
    return list(rights)
