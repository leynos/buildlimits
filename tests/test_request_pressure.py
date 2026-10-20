"""Disposable-process requester pressure and recovery contracts.

Real Unix sockets exercise queued ownership, refusal and accept-time exhaustion.
Run from the repository root with ``uv run pytest -v tests/test_request_pressure.py``.
"""

import array
import contextlib
import os
import selectors
import socket
import typing as typ
from pathlib import Path

import pytest
from conftest import RecordingSink
from descriptor_helpers import fill_table, isolated, open_fds, still_open
from request_helpers import compile_request

from buildlimits.config import SupervisorConfig
from buildlimits.descriptors import close_all
from buildlimits.metrics import Metrics
from buildlimits.protocol import (
    Refused,
    decode_reply,
    encode_request,
    recv_frame,
    send_frame,
)
from buildlimits.requesters import Requests, listen
from buildlimits.supervisor import Supervisor, SupervisorTelemetry

if typ.TYPE_CHECKING:
    from buildlimits.pending_request import Pending


def _connect(listener: socket.socket, rights: list[int]) -> socket.socket:
    """Send one full compile frame with caller-owned descriptor copies."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(listener.getsockname())
    client.settimeout(1)
    send_frame(
        client, encode_request(compile_request(tuple(range(len(rights))))), rights
    )
    return client


def _accept_exhaustion(report: int, path: str) -> object:
    """Queue two requests, exhaust accept headroom, then retry after reclaiming."""
    del report
    now = [0.0]
    sink = RecordingSink()
    supervisor = Supervisor(
        SupervisorConfig(cache_mode="direct"),
        clock=lambda: now[0],
        telemetry=SupervisorTelemetry(metrics=Metrics(sink)),
    )
    clients = []
    filler = []
    with contextlib.ExitStack() as owned:
        owned.callback(supervisor.selector.close)
        listener = owned.enter_context(listen(Path(path)))
        supervisor.listener = listener
        supervisor.selector.register(listener, 1, "listener")
        baseline = open_fds()
        try:
            for _ in range(2):
                clients.append(_connect(listener, []))
                supervisor._accept()
            clients.append(_connect(listener, []))
            filler = fill_table()
            caught = None
            try:
                supervisor._accept()
            except OSError as error:
                caught = error.errno
            health = supervisor.admission.healthy
            failures = supervisor._failures
            close_all(filler)
            filler.clear()
            # A pressure tick must allow a new request without changing health.
            now[0] = 0.1
            supervisor._tick()
            assert supervisor.selector.get_key(listener).data == "listener", (
                "a later tick restores the listener watch"
            )
            supervisor._accept()
            recovered = len(supervisor.requests.pending)
            pressure_metrics = [
                record
                for record in sink.records
                if record["BUILD_LIMITS_EVENT"] == "accept_pressure"
            ]
            recovered_metrics = sink.records[-1]
        finally:
            close_all(filler)
            for request_id in list(supervisor.requests.pending):
                supervisor.requests.forget(request_id)
            for client in clients:
                client.close()
        return {
            "errno": caught,
            "healthy": health,
            "internal_failures": failures,
            "queued_after_recovery": recovered,
            "baseline": open_fds() == baseline,
            "held": supervisor.admission.tokens_held(),
            "pressure_metrics": pressure_metrics,
            "recovered_metrics": recovered_metrics,
        }


def test_accept_exhaustion_preserves_queue_health_and_recovers(tmp_path: Path) -> None:
    """EMFILE on accept is controlled pressure and all queue ownership recovers."""
    result = isolated(
        lambda report: _accept_exhaustion(report, str(tmp_path / "pressure.sock"))
    )
    assert isinstance(result, dict), "the child reports control-pressure evidence"
    assert result["errno"] is None, "accept pressure is handled within the control loop"
    assert result["healthy"] and result["internal_failures"] == 0, (
        "pressure does not count as an internal health failure"
    )
    assert result["queued_after_recovery"] == 3 and result["held"] == 0, (
        "every request is retained without inventing tokens"
    )
    assert result["baseline"], "queue cleanup restores the descriptor baseline"
    _assert_accept_metrics(result)


def _assert_accept_metrics(result: dict[str, typ.Any]) -> None:
    """Verify fixed observations from a real exhausted-table recovery episode."""
    records = result["pressure_metrics"]
    assert [record["BUILD_LIMITS_ACCEPT_STATE"] for record in records] == [
        "paused",
        "resumed",
    ], "real exhaustion produces one bounded pause and one restoration"
    assert records[0]["BUILD_LIMITS_ACCEPT_ERRNO"] == "emfile", (
        "the kernel exhaustion is classified without exposing raw errno data"
    )
    assert records[-1]["BUILD_LIMITS_ACCEPT_PAUSE_SECONDS"] == "0.100", (
        "the successful due retry reports the entire first-pause duration"
    )
    recovered = result["recovered_metrics"]
    assert recovered["BUILD_LIMITS_FD_RETAINED"] == "3", (
        "all three recovered request sockets remain explicitly accounted"
    )
    assert recovered["BUILD_LIMITS_ACCEPT_PAUSED"] == "0", (
        "the recovered snapshot cannot leave the listener labelled paused"
    )


def _refusal(client: socket.socket) -> Refused:
    """Read one actual refusal or fail the experiment on unexpected protocol data."""
    frame = recv_frame(client)
    if frame is None:
        message = "the mismatch experiment lost its refusal"
        raise RuntimeError(message)
    body, rights = frame
    close_all(rights)
    reply = decode_reply(body)
    if not isinstance(reply, Refused):
        message = "the mismatch experiment received an unexpected reply"
        raise TypeError(message)
    return reply


def test_decoded_request_exceeding_total_budget_is_not_retryable() -> None:
    """A final budget refusal closes received rights without queueing a request."""
    baseline = open_fds()
    client, server = socket.socketpair()
    with client, server, selectors.DefaultSelector() as selector:
        requests = Requests(selector, lambda: 0.0)
        requests.descriptors.budget = 3
        rights = [os.open("/dev/null", os.O_RDONLY) for _ in range(3)]
        owned = list(rights)
        try:
            assert (
                requests._admit(server, compile_request((8, 9, 10)), rights) is None
            ), "an idle request larger than the whole budget cannot be recorded"
            owned = still_open(owned)
            assert not owned, (
                "the refusal must close every received right before cleanup"
            )
            reply = _refusal(client)
            assert reply.reason == "descriptors", (
                "permanently oversized requests must not retry as transient pressure"
            )
            assert not requests.pending and not selector.get_map(), (
                "a final refusal retains no request ownership"
            )
        finally:
            close_all(still_open(owned))
    assert open_fds() == baseline, "the final refusal restores the descriptor baseline"


def _receipt_mismatch(
    report: int, path: str, *, truncated: bool, policy_budget: int | None = None
) -> object:
    """Compare a genuine wire mismatch with kernel truncation under pressure."""
    del report
    with selectors.DefaultSelector() as selector, listen(Path(path)) as listener:
        requests = Requests(selector, lambda: 0.0)
        if policy_budget is not None:
            requests.descriptors.budget = policy_budget
        baseline = open_fds()
        filler = []
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(listener.getsockname())
            client.settimeout(1)
            count = 3 if policy_budget is not None else 8 if truncated else 2
            send_frame(
                client,
                encode_request(compile_request(tuple(range(count)))),
                [client.fileno()] * (count if truncated else 1),
            )
            if truncated and policy_budget is None:
                assert requests.descriptors.budget >= 1 + count, (
                    "the kernel-pressure request must fit the total policy budget"
                )
                filler = fill_table()
                for _ in range(3):
                    os.close(filler.pop())

            entry, observed = _accept_observed(requests, listener)
            if entry is not None:
                requests.forget(entry.request_id)
                message = "the experiment unexpectedly recorded mismatched rights"
                raise RuntimeError(message)
            reply = _refusal(client)
            result = {
                "reason": reply.reason,
                "receipt": observed,
                "pending": len(requests.pending),
            }
        finally:
            close_all(filler)
            client.close()
            for request_id in list(requests.pending):
                requests.forget(request_id)
        result["baseline"] = open_fds() == baseline
        return result


def test_policy_truncation_refuses_a_provably_oversized_complete_request(
    tmp_path: Path,
) -> None:
    """Read the bounded declaration before distinguishing permanent policy excess."""
    result = isolated(
        lambda report: _receipt_mismatch(
            report, str(tmp_path / "policy.sock"), truncated=True, policy_budget=3
        )
    )
    assert isinstance(result, dict), "the child reports actual ancillary truncation"
    assert result["receipt"] == [[2, True]], (
        "the policy cap truncates three sent rights"
    )
    assert result["reason"] == "descriptors", (
        "a complete declaration requiring four descriptors cannot fit budget three"
    )
    assert result["pending"] == 0 and result["baseline"], (
        "permanent refusal retains neither requests nor received descriptor ownership"
    )


@pytest.mark.parametrize("truncated", [False, True])
def test_kernel_truncation_and_genuine_sender_mismatch_have_distinct_refusals(
    tmp_path: Path, truncated: bool
) -> None:
    """A receiver-pressure refusal must remain distinct from invalid wire counts."""
    result = isolated(
        lambda report: _receipt_mismatch(
            report, str(tmp_path / "mismatch.sock"), truncated=truncated
        )
    )
    count = 2 if truncated else 1
    assert isinstance(result, dict), "the child reports receipt and refusal evidence"
    assert result["reason"] == ("busy" if truncated else "bad-request"), (
        "receiver pressure and invalid sender counts remain distinct"
    )
    assert result["receipt"] == [[count, truncated]], (
        "the kernel receipt count and truncation flag explain the refusal"
    )
    assert result["pending"] == 0 and result["baseline"], (
        "refusal leaves no request or descriptor ownership"
    )


def _accept_observed(
    requests: Requests, listener: socket.socket
) -> tuple[Pending | None, list[tuple[int, bool]]]:
    """Observe actual kernel receipt without changing rights ownership.

    Returns
    -------
    tuple[Pending | None, list[tuple[int, bool]]]
        Recorded request, if any, and ancillary counts/truncation flags.
    """
    original = socket.socket.recvmsg
    observed: list[tuple[int, bool]] = []

    def receive(
        sock: socket.socket, size: int, ancillary_size: int, flags: int = 0
    ) -> tuple[bytes, list[tuple[int, int, bytes]], int, object]:
        """Record actual ancillary flags and counts without changing receipt."""
        result = original(sock, size, ancillary_size, flags)
        count = sum(
            len(value) // array.array("i").itemsize
            for level, kind, value in result[1]
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS
        )
        if count or result[2] & socket.MSG_CTRUNC:
            observed.append((count, bool(result[2] & socket.MSG_CTRUNC)))
        return result

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, "recvmsg", receive)
        entry = requests.accept(listener, lambda: {})
    return entry, observed
