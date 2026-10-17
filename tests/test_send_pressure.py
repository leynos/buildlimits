"""Sender-side SCM_RIGHTS pressure retries only before any request transfer."""

import contextlib
import errno
import os
import resource
import socket
import typing as typ
from pathlib import Path

import pytest
from adapter_doubles import immediate_retry, script_adapter
from descriptor_helpers import isolated, open_fds

from buildlimits import adapter, adapter_metrics, framing
from buildlimits.journal_native import JournalSink
from buildlimits.protocol import Admitted, Exit, Finished, encode_reply

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _inflight(_report: int) -> object:
    """Hold bounded rights in a private socket queue, then receive and reclaim them."""
    baseline = open_fds()
    sender, receiver = socket.socketpair()
    number = None
    sent = 0
    with sender, receiver, Path(os.devnull).open("rb") as source:
        sent, number = _queue_rights(sender, source.fileno())
        count = len(open_fds())
        for _message in range(sent):
            _receive_rights(receiver, b"x", 8)
        socket.send_fds(sender, [b"y"], [source.fileno()])
        _receive_rights(receiver, b"y", 1)
    return {
        "errno": number,
        "sent": sent,
        "open": count,
        "soft": resource.getrlimit(resource.RLIMIT_NOFILE)[0],
        "baseline": open_fds() == baseline,
    }


def test_inflight_rights_exhaustion_is_distinct_from_open_descriptor_exhaustion() -> (
    None
):
    """Queued ancillary rights can hit the caller limit despite a sparse FD table."""
    result = isolated(_inflight, soft=64)
    assert isinstance(result, dict), "the disposable child reports kernel evidence"
    if result["errno"] is None:
        pytest.skip("privileged in-flight limit exemption; not acceptance evidence")
    assert result["errno"] == errno.ETOOMANYREFS, result
    assert result["open"] < result["soft"], "open descriptors did not exhaust the table"
    assert result["baseline"], "all queued rights and sockets return to baseline"


def test_first_send_pressure_retries_without_transferring_or_duplicating_request(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """A refused initial rights send reconnects and executes exactly one request."""
    baseline = open_fds()
    captures: list[tuple[int, ...]] = []
    sends: list[int] = []
    with contextlib.ExitStack() as owned:
        journal = owned.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM))
        journal_path = tmp_path / "journal"
        journal.bind(str(journal_path))
        journal.settimeout(0.05)
        sink = JournalSink(journal_path, identifier="build-limits-adapter")
        monkeypatch.setattr(adapter_metrics, "JournalSink", lambda **_kwargs: sink)
        pairs = [socket.socketpair() for _attempt in range(2)]
        for client, server in pairs:
            owned.enter_context(client)
            owned.enter_context(server)
        source = owned.enter_context(Path(os.devnull).open("rb"))
        connections = iter(client for client, _server in pairs)
        pairs[1][1].sendall(encode_reply(Admitted("g1", False)))
        pairs[1][1].sendall(encode_reply(Finished(Exit(code=31))))
        original = socket.send_fds

        def send(
            sock: socket.socket, buffers: cabc.Sequence[bytes], fds: list[int]
        ) -> int:
            """Reject only the first ancillary send before calling the kernel."""
            sends.append(sock.fileno())
            if len(sends) == 1:
                raise OSError(errno.ETOOMANYREFS, "injected in-flight pressure")
            return original(sock, buffers, fds)

        def capture(exclude: cabc.Collection[int]) -> list[int]:
            """Recapture a borrowed caller descriptor for each fresh attempt."""
            captures.append(tuple(exclude))
            return [source.fileno()]

        monkeypatch.setattr(socket, "send_fds", send)
        script_adapter(monkeypatch, connections, capture)
        retry = immediate_retry()
        assert adapter.run(["rustc"], cacheable=False, retry=retry) == 31, (
            "only completion ends retry"
        )
        record = journal.recv(8192)
        assert b"BUILD_LIMITS_EVENT=adapter_finished\n" in record, (
            "the invocation emits one native summary after all retries"
        )
        assert b"BUILD_LIMITS_OUTCOME=completed\n" in record, (
            "transient pressure does not replace the completed outcome"
        )
        with pytest.raises(TimeoutError):
            journal.recv(8192)
        pairs[0][1].settimeout(1.0)
        assert pairs[0][1].recv(1) == b"", (
            "the failed send transferred no request bytes"
        )
        assert len(captures) == len(sends) == 2, (
            "retry recaptures on a fresh connection"
        )
        os.fstat(source.fileno())
    assert open_fds() == baseline, (
        "retry closes both connections without owning caller FDs"
    )
    assert capsys.readouterr().err.count("waiting for descriptor headroom") == 1, (
        "sender pressure reports one bounded waiting line"
    )


def test_partial_frame_failure_is_never_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once an initial send succeeds, later write errors cannot duplicate admission."""

    class PartialSocket:
        """Fail only the body write after a successful first rights transfer."""

        def sendall(self, _data: bytes) -> None:
            """Surface a later write error without permitting replay."""
            raise OSError(errno.ETOOMANYREFS, "injected later write failure")

    monkeypatch.setattr(socket, "send_fds", lambda *_args: 1)
    with pytest.raises(OSError, match="injected later write failure") as caught:
        framing.send_frame(typ.cast("socket.socket", PartialSocket()), b"frame", [0])
    assert caught.value.errno == errno.ETOOMANYREFS, "the later error stays final"


@pytest.mark.parametrize("number", [errno.EBADF, errno.EINVAL, errno.EMFILE])
def test_initial_non_pressure_errors_remain_final(
    monkeypatch: pytest.MonkeyPatch, number: int
) -> None:
    """Only the demonstrated in-flight refusal permits an initial transfer retry."""

    def fail(*_args: object) -> int:
        """Reject the initial send with an unrelated descriptor error."""
        raise OSError(number, "injected initial failure")

    monkeypatch.setattr(socket, "send_fds", fail)
    client, server = socket.socketpair()
    with client, server:
        with pytest.raises(OSError, match="injected initial failure") as caught:
            framing.send_frame(client, b"frame", [0])
    assert caught.value.errno == number, "unrelated errors retain their original cause"


def _queue_rights(sender: socket.socket, source: int) -> tuple[int, int | None]:
    """Queue bounded ancillary messages until the kernel refuses further rights.

    Returns
    -------
    tuple[int, int | None]
        Successful send count and observed kernel errno, if any.
    """
    sent = 0
    for _attempt in range(32):
        try:
            socket.send_fds(sender, [b"x"], [source] * 8)
        except OSError as error:
            return sent, error.errno
        sent += 1
    return sent, None


def _receive_rights(receiver: socket.socket, expected: bytes, count: int) -> None:
    """Check and close every descriptor installed by one successful queued send."""
    data, rights, _flags, _address = socket.recv_fds(receiver, 1, count)
    try:
        assert data == expected and len(rights) == count, (
            "every send is received intact"
        )
    finally:
        for descriptor in rights:
            os.close(descriptor)
