"""Requester recording and completion reclaim resources across later faults."""

import collections
import errno
import os
import selectors
import socket
import typing as typ

import pytest
from descriptor_helpers import isolated, open_fds
from request_helpers import compile_request

from buildlimits import requesters
from buildlimits.descriptors import close_all
from buildlimits.protocol import Exit, encode_request, send_frame
from buildlimits.requesters import Pending, Requests

if typ.TYPE_CHECKING:
    from pathlib import Path


class _FailingPending(collections.UserDict[int, Pending]):
    """Reject insertion after the accepted connection has been registered."""

    def __setitem__(self, key: int, item: Pending) -> None:
        """Inject a commit failure before retaining the pending entry."""
        del key, item
        msg = "injected pending commit failure"
        raise RuntimeError(msg)


def _send_compile(listener: socket.socket) -> socket.socket:
    """Connect one real request with a right owned by its receiver."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(listener.getsockname())
        send_frame(client, encode_request(compile_request((8,))), [client.fileno()])
    except BaseException:
        client.close()
        raise
    return client


def _post_registration_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """Later recording faults close received rights and leave no selector key."""
    baseline = open_fds()
    with (
        selectors.DefaultSelector() as selector,
        requesters.listen(tmp_path / "request.sock") as listener,
        _send_compile(listener) as client,
    ):
        requests = Requests(selector, clock=lambda: 0.0)
        del client
        if stage == "pending":
            requests.pending = typ.cast("dict[int, Pending]", _FailingPending())
        else:

            def fail(_retained: int) -> None:
                """Inject a fault after pending ownership was committed."""
                msg = "injected observation failure"
                raise RuntimeError(msg)

            monkeypatch.setattr(requests.descriptors, "observe", fail)
        empty = open_fds()
        try:
            with pytest.raises(RuntimeError, match="injected"):
                requests.accept(listener, lambda: {})
            assert not requests.pending, "failed recording owns no pending entry"
            assert not selector.get_map(), "failed recording owns no selector watch"
            assert open_fds() == empty, (
                "the accepted socket and received right close exactly once"
            )
        finally:
            for entry in requests.pending.values():
                close_all(entry.fds)
                if entry.conn is not None:
                    entry.conn.close()
    assert open_fds() == baseline, "the full transaction restores its caller's baseline"


def _rejection_logging_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reclaim malformed-request ownership even when the journal fails."""
    with (
        selectors.DefaultSelector() as selector,
        requesters.listen(tmp_path / "rejected.sock") as listener,
    ):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with client:
            client.connect(listener.getsockname())
            send_frame(client, b"\x00\x00\x00\x01{", [client.fileno()])
            requests = Requests(selector, clock=lambda: 0.0)
            baseline = open_fds()

            def fail(_message: str) -> None:
                """Fail a nonessential journal write after decoding was rejected."""
                raise BrokenPipeError(errno.EPIPE, "injected journal failure")

            monkeypatch.setattr(requesters, "log", fail)
            assert requests.accept(listener, lambda: {}) is None, (
                "malformed requests remain controlled rejections"
            )
            assert not requests.pending and not selector.get_map(), (
                "no rejection owns a recorded resource"
            )
            assert open_fds() == baseline, (
                "every received right and accepted socket is reclaimed"
            )


def _completion_logging_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forget completed requester ownership despite a journal failure."""
    baseline = open_fds()
    client, server = socket.socketpair()
    with client, server, selectors.DefaultSelector() as selector:
        requests = Requests(selector, clock=lambda: 0.0)
        source = os.open("/dev/null", os.O_RDONLY)
        entry = requests._admit(server, compile_request((8,)), [source])
        assert entry is not None, "the request is recorded with an owned right"

        def fail(_message: str) -> None:
            """Inject a journal failure after the completion reply was sent."""
            raise BrokenPipeError(errno.EPIPE, "injected journal failure")

        monkeypatch.setattr(requesters, "log", fail)
        try:
            with pytest.raises(BrokenPipeError, match="injected"):
                requests.finish(entry.request_id, Exit(code=0))
            assert not requests.pending and not selector.get_map(), (
                "completion forgets requester ownership despite diagnostic failure"
            )
            with pytest.raises(OSError, match="Bad file descriptor") as caught:
                os.fstat(source)
            assert caught.value.errno == errno.EBADF, "the received right was closed"
        finally:
            requests.forget(entry.request_id)
    assert open_fds() == baseline, "completion returns every descriptor to baseline"


@pytest.mark.parametrize("stage", ["pending", "observe"])
def test_post_registration_fault_rolls_back_every_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """Disposable recording faults preserve parent limits and descriptor ownership."""
    assert (
        isolated(lambda _report: _post_registration_fault(tmp_path, monkeypatch, stage))
        is None
    ), "the disposable transaction verifies complete resource recovery"


def test_rejection_logging_failure_cannot_leak_accepted_rights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disposable malformed-request cleanup survives a failed diagnostic write."""
    assert (
        isolated(lambda _report: _rejection_logging_failure(tmp_path, monkeypatch))
        is None
    ), "the disposable transaction verifies complete resource recovery"


def test_completion_logging_failure_still_forgets_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disposable completion faults restore the caller's exact ownership baseline."""
    assert isolated(lambda _report: _completion_logging_failure(monkeypatch)) is None, (
        "the disposable completion verifies exact ownership recovery"
    )
