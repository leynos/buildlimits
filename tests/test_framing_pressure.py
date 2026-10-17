"""Reproduce received-descriptor leaks and receiver headroom truncation.

Disposable low-limit processes exercise partial-header and body timeouts and
SCM_RIGHTS truncation without changing the parent's descriptor table or limits.
Run from the repository root with ``uv run pytest -v tests/test_framing_pressure.py``.
"""

import errno
import socket

import pytest
from descriptor_helpers import fill_table, isolated, open_fds

from buildlimits import framing
from buildlimits.descriptors import close_all


def _timeout(report: int, header: bytes) -> object:
    """Time out after receiving rights with the supplied incomplete frame."""
    del report
    left, right = socket.socketpair()
    with left, right:
        right.settimeout(0.01)
        baseline = open_fds()
        socket.send_fds(left, [header], [left.fileno()])
        try:
            frame = framing.recv_frame(right)
        except TimeoutError:
            return {"leaked": sorted(open_fds() - baseline)}
        if frame is not None:
            close_all(frame[1])
        return {"error": "unexpected-success"}


@pytest.mark.parametrize(
    "header",
    [
        pytest.param(
            b"\0",
            id="partial-header",
        ),
        pytest.param(
            b"\0\0\0\x10",
            id="complete-header-incomplete-body",
        ),
    ],
)
def test_receipt_timeout_closes_received_descriptors(header: bytes) -> None:
    """A timeout must release rights received in either header or body reading."""
    assert isolated(lambda report: _timeout(report, header)) == {"leaked": []}, (
        "receipt timeout must not leak"
    )


def _truncated(report: int) -> object:
    """Leave two free slots for a frame carrying eight descriptors."""
    del report
    left, right = socket.socketpair()
    with left, right:
        framing.send_frame(
            left, framing.encode_frame({"type": "status"}), [left.fileno()] * 8
        )
        filled = fill_table()
        close_all(filled[-2:])
        try:
            result = framing.recv_frame(right)
        except framing.ProtocolError as error:
            return {"error": type(error).__name__}
        assert result is not None, "a complete frame arrives"
        _, received = result
        close_all(received)
        return {"received": len(received)}


def test_receiver_headroom_truncation_is_explicit() -> None:
    """Receiver RLIMIT exhaustion truncates rights; it is not a sender mismatch."""
    assert isolated(_truncated) == {"error": "DescriptorTruncationError"}, (
        "truncation must be explicit"
    )


def _body_error(report: int) -> object:
    """Inject an I/O error after the header has transferred one owned right."""
    del report
    left, right = socket.socketpair()
    with left, right, pytest.MonkeyPatch.context() as patch:
        baseline = open_fds()
        socket.send_fds(left, [b"\0\0\0\x10"], [left.fileno()])
        original = socket.socket.recvmsg
        calls = 0

        def receive(
            sock: socket.socket, size: int, ancillary_size: int, flags: int = 0
        ) -> tuple[bytes, list[tuple[int, int, bytes]], int, object]:
            """Receive the header normally and fail the subsequent body read."""
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(errno.EIO, "injected body receipt error")
            return original(sock, size, ancillary_size, flags)

        patch.setattr(socket.socket, "recvmsg", receive)
        try:
            frame = framing.recv_frame(right)
        except OSError as error:
            return {"errno": error.errno, "leaked": sorted(open_fds() - baseline)}
        if frame is not None:
            close_all(frame[1])
        return {"error": "unexpected-success"}


def test_body_io_failure_closes_header_descriptors() -> None:
    """Body receipt failure must close every descriptor received with its header."""
    assert isolated(_body_error) == {"errno": errno.EIO, "leaked": []}, (
        "header ownership ends on body failure"
    )
