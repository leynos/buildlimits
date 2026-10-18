"""Tests for how the supervisor accepts requests and their descriptors."""

import os
import selectors
import socket
import typing as typ
from pathlib import Path

import pytest

from buildlimits.attributes import AttributePlan, ProcessAttributes
from buildlimits.protocol import (
    CompileRequest,
    Exit,
    StatusReport,
    StatusRequest,
    decode_reply,
    encode_frame,
    encode_request,
    recv_frame,
    send_frame,
)
from buildlimits.requesters import Pending, Requests, listen

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _open_descriptors() -> set[int]:
    """List the descriptors this process has open."""
    return {int(entry.name) for entry in Path("/proc/self/fd").iterdir()}


@pytest.fixture
def listener(tmp_path: Path) -> cabc.Iterator[socket.socket]:
    """Provide the supervisor's listening socket in a private directory."""
    sock = listen(tmp_path / "run" / "supervisor.sock")
    yield sock
    sock.close()


def _send(listener: socket.socket, frame: bytes) -> socket.socket:
    """Connect as a client and send one frame carrying a descriptor."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(listener.getsockname())
    read_end, write_end = os.pipe()
    send_frame(client, frame, [read_end])
    os.close(read_end)
    os.close(write_end)
    return client


def _accept(listener: socket.socket) -> set[int]:
    """Accept one request and report the descriptors it left open."""
    with selectors.DefaultSelector() as selector:
        requests = Requests(selector, clock=lambda: 0.0)
        before = _open_descriptors()
        entry = requests.accept(listener, status=lambda: {"healthy": True})
        assert entry is None, "the request is not recorded"
        return _open_descriptors() - before


def test_a_rejected_request_closes_its_descriptors(listener: socket.socket) -> None:
    """A frame that does not decode leaves none of its descriptors behind."""
    client = _send(listener, encode_frame({"type": "unknown"}))
    try:
        assert _accept(listener) == set(), "a rejected request leaks no descriptor"
        assert client.recv(1) == b"", "the rejected connection is closed"
    finally:
        client.close()


def test_a_status_request_closes_its_descriptors(listener: socket.socket) -> None:
    """A status request is answered and any descriptors it carried are closed."""
    client = _send(listener, encode_request(StatusRequest()))
    try:
        assert _accept(listener) == set(), "a status request leaks no descriptor"
        frame = recv_frame(client)
        assert frame is not None, "the status request is answered"
        body, _fds = frame
        assert decode_reply(body) == StatusReport({"healthy": True}), (
            "the answer is the status report"
        )
    finally:
        client.close()


def _secret_pending(
    request_id: int,
    conn: socket.socket | None,
    *,
    arrived: float,
    admitted: float | None = None,
) -> Pending:
    """Record a request whose identifying fields must never reach a metric.

    Returns
    -------
    Pending
        A request carrying a secret label, directory and environment value.
    """
    request = CompileRequest(
        ("rustc",),
        "/secret/crate",
        {"TOKEN": "private"},
        ProcessAttributes(0o22, 0, {}),
        (),
        "secret",
    )
    return Pending(
        request_id,
        conn,
        request,
        [],
        AttributePlan(0o22, 0, {}),
        arrived=arrived,
        admitted=admitted,
    )


@pytest.mark.parametrize("ending", ["finish", "refuse"])
def test_terminal_metrics_measure_zero_admission_time(
    metrics, metric_sink, ending
) -> None:
    """An admitted timestamp of zero is present and is not an arrival fallback."""
    with selectors.DefaultSelector() as selector:
        requests = Requests(selector, clock=lambda: 5.0, metrics=metrics)
        requests.pending[1] = _secret_pending(1, None, arrived=-2.0, admitted=0.0)
        if ending == "refuse":
            requests.refuse(1, "launch-failed")
            requests.refuse(1, "launch-failed")
        else:
            requests.finish(1, Exit(code=0))
            requests.finish(1, Exit(code=0))
    assert len(metric_sink.records) == 1, "forgotten requests emit nothing"
    record = metric_sink.records[0]
    assert record["BUILD_LIMITS_WAIT_SECONDS"] == "2.000", (
        "zero is a valid admission time"
    )
    assert record["BUILD_LIMITS_RUNTIME_SECONDS"] == "5.000", (
        "runtime starts at admission"
    )
    assert "secret" not in str(record) and "private" not in str(record), (
        "identity-free metrics"
    )


def test_disconnect_emits_one_terminal_record(metrics, metric_sink) -> None:
    """Queued cancellations finish now; admitted cancellations finish after drain."""
    with selectors.DefaultSelector() as selector:
        requests = Requests(selector, clock=lambda: 4.0, metrics=metrics)
        for number in (1, 2):
            client, server = socket.socketpair()
            requests.pending[number] = _secret_pending(number, server, arrived=0.0)
            selector.register(server, selectors.EVENT_READ)
            client.close()
            assert requests.disconnected(number) is not None, "disconnect noticed"
        requests.pending[2].admitted = 0.0
        requests.cancel_queued(1)
        requests.finish(2, Exit(signal=15))
        requests.cancel_queued(1)
    assert len(metric_sink.records) == 2, "each request finishes once"
    assert all(
        r["BUILD_LIMITS_CANCELLATION"] == "disconnect" for r in metric_sink.records
    ), "cause kept"
    assert all(r["BUILD_LIMITS_OUTCOME"] == "cancelled" for r in metric_sink.records), (
        "cancellations classified"
    )
