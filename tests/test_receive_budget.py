"""Strict Linux ancillary ceilings preserve reserved descriptor headroom."""

import fcntl
import socket
import time
import typing as typ

import pytest
from descriptor_helpers import isolated, open_fds, still_open

from buildlimits import framing
from buildlimits.descriptors import close_all

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion


def _receive_budget(_report: int, ceiling: int, *, exceeds: bool) -> object:
    """Receive rights within an exact ceiling and verify ownership recovery."""
    sender, receiver = socket.socketpair()
    with sender, receiver:
        baseline = open_fds()
        count = ceiling + int(exceeds)
        framing.send_frame(
            sender, framing.encode_frame({"type": "status"}), [sender.fileno()] * count
        )
        try:
            result = framing.recv_frame(receiver, max_fds=ceiling)
        except framing.ProtocolError as error:
            outcome: dict[str, object] = {"error": type(error).__name__}
        else:
            assert result is not None, "the complete frame is available"
            _, rights = result
            outcome = {
                "received": len(rights),
                "cloexec": all(
                    fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC for fd in rights
                ),
            }
            close_all(rights)
        outcome["baseline"] = open_fds() == baseline
        return outcome


@pytest.mark.parametrize("ceiling", [0, 1, 2, 3, 4, 5])
@pytest.mark.parametrize("exceeds", [False, True])
def test_frame_receive_ceiling_is_exact(ceiling: int, *, exceeds: bool) -> None:
    """Odd ancillary budgets cannot consume an extra alignment-padding slot."""
    result = isolated(lambda report: _receive_budget(report, ceiling, exceeds=exceeds))
    expected = (
        {"error": "DescriptorTruncationError"}
        if exceeds
        else {"received": ceiling, "cloexec": True}
    )
    expected["baseline"] = True
    assert result == expected, (
        "receipt obeys its exact cap and closes every owned right"
    )


def _default_flags(_report: int) -> object:
    """Check close-on-exec receipt through the existing default frame API."""
    sender, receiver = socket.socketpair()
    with sender, receiver:
        baseline = open_fds()
        framing.send_frame(
            sender, framing.encode_frame({"type": "status"}), [sender.fileno()]
        )
        frame = framing.recv_frame(receiver)
        assert frame is not None, "the frame arrives with its owned right"
        rights = frame[1]
        outcome = {
            "received": len(rights),
            "cloexec": all(
                fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC for fd in rights
            ),
        }
        close_all(rights)
        outcome["baseline"] = open_fds() == baseline
        return outcome


def test_default_receipt_sets_close_on_exec() -> None:
    """Received rights cannot escape through an unrelated supervisor exec."""
    assert isolated(_default_flags) == {
        "received": 1,
        "cloexec": True,
        "baseline": True,
    }, "every received right is close-on-exec"


def _deadline(_report: int, *, truncated: bool = False) -> object:
    """Expire one shared frame deadline with intact or truncated rights."""
    sender, receiver = socket.socketpair()
    with sender, receiver, pytest.MonkeyPatch.context() as patch:
        receiver.settimeout(10)
        baseline = open_fds()
        framing.send_frame(
            sender,
            framing.encode_frame({"type": "status"}),
            [sender.fileno()] * (1 + int(truncated)),
        )
        clock = iter([100.0, 104.0, 111.0])
        patch.setattr(time, "monotonic", lambda: next(clock))
        try:
            frame = framing.recv_frame(
                receiver, max_fds=1 if truncated else framing.MAX_FDS
            )
        except TimeoutError:
            outcome = {"error": "TimeoutError", "timeout": receiver.gettimeout()}
        else:
            if frame is not None:
                close_all(frame[1])
            outcome = {"error": "unexpected-success", "timeout": receiver.gettimeout()}
        outcome["baseline"] = open_fds() == baseline
        return outcome


def test_frame_deadline_is_shared_by_header_and_body() -> None:
    """A new body read cannot reset the request's total receive deadline."""
    assert isolated(_deadline) == {
        "error": "TimeoutError",
        "timeout": 10.0,
        "baseline": True,
    }, "deadline expiry closes header rights and restores the original timeout"


def _truncated_frame(_report: int) -> object:
    """Observe ownership before body receipt and discard subsequent rights."""
    sender, receiver = socket.socketpair()
    with sender, receiver, pytest.MonkeyPatch.context() as patch:
        receiver.settimeout(10)
        baseline = open_fds()
        frame = framing.encode_frame({"type": "status", "secret": "private-payload"})
        socket.send_fds(sender, [frame[:1]], [sender.fileno()])
        socket.send_fds(sender, [frame[1:4]], [sender.fileno()] * 2)
        socket.send_fds(sender, [frame[4:]], [sender.fileno()])
        original = socket.socket.recvmsg
        installed: list[int] = []
        closed_before_body: list[bool] = []
        outcome: dict[str, object]

        def receive(
            sock: socket.socket, size: int, ancillary_size: int, flags: int = 0
        ) -> tuple[bytes, list[tuple[int, int, bytes]], int, object]:
            """Check installed header rights before any following receive blocks."""
            if installed:
                closed_before_body.append(not still_open(installed))
            result = original(sock, size, ancillary_size, flags)
            installed.extend(framing._received_rights(result[1]))
            return result

        patch.setattr(socket.socket, "recvmsg", receive)
        try:
            result = framing.recv_frame(receiver, max_fds=2)
        except framing.DescriptorTruncationError as error:
            outcome = {
                "body": error.body == frame[4:],
                "private": "private-payload"
                not in str(error) + repr(error.args) + repr(error),
                "immediate": closed_before_body == [False, True],
                "timeout": receiver.gettimeout(),
            }
        else:
            if result is not None:
                close_all(result[1])
            outcome = {"error": "unexpected-success"}
        outcome["baseline"] = open_fds() == baseline
        return outcome


def test_truncation_retains_only_complete_body_and_closes_rights_immediately(
    snapshot: SnapshotAssertion,
) -> None:
    """A complete truncated frame permits policy checks without retaining rights."""
    result = isolated(_truncated_frame)
    assert isinstance(result, dict), "the child reports complete receipt evidence"
    assert result["body"], "only the complete original body is exposed for policy"
    assert result["private"], "exception diagnostics must not disclose the payload"
    assert result["immediate"] and result["baseline"], (
        "header and later rights close before further receipt without leaking"
    )
    assert result == snapshot, "the bounded receipt evidence changed"


def _incomplete_truncation(_report: int, header: bytes, *, eof: bool) -> object:
    """Lose rights in an incomplete frame, then encounter EOF or deadline expiry."""
    sender, receiver = socket.socketpair()
    outcome: dict[str, object]
    with sender, receiver:
        receiver.settimeout(0.01)
        baseline = open_fds()
        socket.send_fds(sender, [header], [sender.fileno()] * 2)
        if eof:
            sender.shutdown(socket.SHUT_WR)
        try:
            result = framing.recv_frame(receiver, max_fds=1)
        except (framing.ProtocolError, TimeoutError) as error:
            outcome = {"error": type(error).__name__, "timeout": receiver.gettimeout()}
        else:
            if result is not None:
                close_all(result[1])
            outcome = {"error": "unexpected-success"}
        outcome["baseline"] = open_fds() == baseline
        return outcome


@pytest.mark.parametrize(
    "header", [b"\0", b"\0\0\0\x10"], ids=["partial-header", "missing-body"]
)
@pytest.mark.parametrize("eof", [False, True], ids=["deadline", "eof"])
def test_incomplete_truncation_does_not_supply_a_policy_body(
    header: bytes, snapshot: SnapshotAssertion, *, eof: bool
) -> None:
    """Incomplete frames preserve EOF/timeout errors rather than guessing policy."""
    result = isolated(lambda report: _incomplete_truncation(report, header, eof=eof))
    assert isinstance(result, dict), "the child reports incomplete receipt evidence"
    assert result["error"] == ("ProtocolError" if eof else "TimeoutError"), (
        "incomplete receipt must not become a complete-body truncation refusal"
    )
    assert result["baseline"], "all received rights return to the initial baseline"
    assert result["timeout"] == pytest.approx(0.01), (
        "receipt restores the original socket timeout"
    )
    assert result == snapshot, "the bounded incomplete-receipt evidence changed"


def test_truncated_receipt_preserves_the_original_total_deadline() -> None:
    """Draining a truncated frame cannot grant the sender a fresh body timeout."""
    assert isolated(lambda report: _deadline(report, truncated=True)) == {
        "error": "TimeoutError",
        "timeout": 10.0,
        "baseline": True,
    }, "truncation drains only within the existing frame deadline"
