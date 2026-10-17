"""Targeted evidence distinguishing descriptor transport and count failures."""

import errno
import os
import socket
from pathlib import Path

import pytest
from descriptor_helpers import fill_table, isolated, open_fds

from buildlimits import framing
from buildlimits.descriptors import close_all, inheritable_descriptors


def _raw_truncation(report: int) -> object:
    """Observe kernel flags with exactly two descriptor slots available."""
    del report
    left, right = socket.socketpair()
    with left, right:
        baseline = open_fds()
        socket.send_fds(left, [b"frame"], [left.fileno()] * 8)
        filled = fill_table()
        for _ in range(2):
            os.close(filled.pop())
        _body, received, flags, _address = socket.recv_fds(
            right, 4096, 253, socket.MSG_CMSG_CLOEXEC
        )
        count = len(received)
        close_all(received)
        close_all(filled)
        return {
            "received": count,
            "truncated": bool(flags & socket.MSG_CTRUNC),
            "baseline": open_fds() == baseline,
        }


def test_low_headroom_sets_ctrunc_instead_of_silently_losing_sender_rights() -> None:
    """Two of eight rights arrive with an explicit kernel truncation flag."""
    assert isolated(_raw_truncation) == {
        "received": 2,
        "truncated": True,
        "baseline": True,
    }, "kernel truncation is independently observed and all owned handles close"


def _too_many(report: int) -> object:
    """Make more than 253 actual inheritable descriptors before framing."""
    left, right = socket.socketpair()
    with left, right:
        source = os.open("/dev/null", os.O_RDWR)
        copies = [os.dup(source) for _ in range(254)]
        try:
            for fd in copies:
                os.set_inheritable(fd, True)
            fds = inheritable_descriptors(
                exclude={report, left.fileno(), right.fileno()}
            )
            assert len(fds) >= 254, "the transport input exceeds the Linux rights cap"
            with pytest.raises(framing.ProtocolError, match="limit is 253"):
                framing.send_frame(left, framing.encode_frame({"type": "status"}), fds)
            right.setblocking(False)
            with pytest.raises(BlockingIOError):
                right.recv(1)
        finally:
            close_all(copies)
            os.close(source)
    return {"refused_before_send": True}


def test_more_than_253_inheritable_descriptors_are_refused_before_send() -> None:
    """The sender's explicit transport cap is distinct from receipt mismatch."""
    assert isolated(_too_many, soft=512) == {"refused_before_send": True}, (
        "an oversized rights list never reaches the receiver"
    )


def test_closure_between_enumeration_and_send_is_ebadf() -> None:
    """A vanished source fails sendmsg atomically, rather than arriving short."""
    left, right = socket.socketpair()
    with left, right:
        source = os.open("/dev/null", os.O_RDWR)
        os.set_inheritable(source, True)
        assert source in inheritable_descriptors(), (
            "the sender initially observes the source"
        )
        os.close(source)
        with pytest.raises(OSError, match=r"\[Errno 9\]") as failure:
            framing.send_frame(left, framing.encode_frame({"type": "status"}), [source])
        assert failure.value.errno == errno.EBADF, (
            "the missing source is a sender syscall failure"
        )
        right.setblocking(False)
        with pytest.raises(BlockingIOError):
            right.recv(1)


def _enumeration(report: int) -> object:
    """Distinguish a raw listing's own directory slot from verified handles."""
    del report
    raw = {int(item.name) for item in Path("/proc/self/fd").iterdir()}
    verified = open_fds()
    return {"extra": len(raw - verified), "missing": len(verified - raw)}


def test_raw_proc_listing_counts_its_transient_directory_descriptor() -> None:
    """A single-threaded disposable process exhibits a one-slot listing excess."""
    assert isolated(_enumeration) == {"extra": 1, "missing": 0}, (
        "raw enumeration includes its own now-closed directory handle"
    )
