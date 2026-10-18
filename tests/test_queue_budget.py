"""Low-limit queue budgets recover after repeated real-socket cancellation."""

import contextlib
import os
import socket
from pathlib import Path

from descriptor_helpers import isolated, open_fds, still_open
from request_helpers import compile_request

from buildlimits.config import SupervisorConfig
from buildlimits.protocol import (
    Refused,
    decode_reply,
    encode_request,
    recv_frame,
    send_frame,
)
from buildlimits.requesters import listen
from buildlimits.supervisor import Supervisor


def _fill_queue(
    supervisor: Supervisor,
    source: int,
    clients: list[socket.socket],
    owned: contextlib.ExitStack,
) -> str:
    """Submit until one real request receives controlled descriptor backpressure."""
    for _attempt in range(16):
        client = owned.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        clients.append(client)
        client.connect(str(supervisor.settings.socket_path))
        client.settimeout(1)
        send_frame(
            client, encode_request(compile_request(tuple(range(6)))), [source] * 6
        )
        supervisor._accept()
        frame = recv_frame(client)
        assert frame is not None, "every request receives progress or refusal"
        reply = decode_reply(frame[0])
        if isinstance(reply, Refused):
            client.close()
            clients.pop()
            return reply.reason
    msg = "the low-limit queue did not apply bounded backpressure"
    raise AssertionError(msg)


def _cancel_queue(
    supervisor: Supervisor, clients: list[socket.socket], baseline: set[int]
) -> dict[str, object]:
    """Disconnect real clients and inspect exact request-resource recovery."""
    retained = supervisor.requests.retained
    rights = [fd for entry in supervisor.requests.pending.values() for fd in entry.fds]
    for conn in clients:
        conn.close()
    clients.clear()
    for request_id in list(supervisor.requests.pending):
        supervisor._connection_ready(request_id)
    supervisor._failed_replies()
    return {
        "retained": retained,
        "budget": supervisor.requests.descriptors.budget,
        "gone": not still_open(rights),
        "baseline": open_fds() == baseline,
    }


def _cycle(_report: int, path: str) -> object:
    """Saturate and cancel twelve queues inside an isolated low-limit process."""
    baseline = open_fds()
    supervisor = Supervisor(
        SupervisorConfig(slots=1, cache_mode="direct", socket_path=Path(path))
    )
    rounds: list[dict[str, object]] = []
    clients: list[socket.socket] = []
    with contextlib.ExitStack() as owned:
        owned.callback(supervisor.selector.close)
        listener = owned.enter_context(listen(Path(path)))
        supervisor.listener = listener
        source = os.open("/dev/null", os.O_RDWR)
        owned.callback(os.close, source)
        supervisor.requests.descriptors.configure(slots=1)
        empty = open_fds()
        try:
            for _ in range(12):
                rounds.append({
                    "refusal": _fill_queue(supervisor, source, clients, owned),
                    **_cancel_queue(supervisor, clients, empty),
                })
            result = {
                "rounds": rounds,
                "pending": len(supervisor.requests.pending),
                "queue": len(supervisor.admission.queue),
                "held": supervisor.admission.tokens_held(),
                "healthy": supervisor.admission.healthy,
                "descriptors": supervisor.status()["descriptors"],
            }
        finally:
            supervisor.requests.close_all()
            for client in clients:
                client.close()
    result["baseline"] = open_fds() == baseline
    return result


def test_queue_budget_refuses_without_health_loss_and_recovers(tmp_path: Path) -> None:
    """Repeated busy/cancel cycles preserve exact baselines and token accounting."""
    result = isolated(lambda report: _cycle(report, str(tmp_path / "budget.sock")))
    assert isinstance(result, dict), "the child reports structured pressure evidence"
    assert len(result["rounds"]) == 12, (
        "repeated cycles distinguish retention from leakage"
    )
    for round_ in result["rounds"]:
        assert round_["refusal"] == "busy", "descriptor pressure is retryable"
        assert 0 < round_["retained"] <= round_["budget"], (
            "retention respects its budget"
        )
        assert round_["gone"] and round_["baseline"], (
            "cancellation releases owned rights"
        )
    assert result["pending"] == 0 and result["queue"] == 0, (
        "no cancelled ownership remains"
    )
    assert result["held"] == 0 and result["healthy"], (
        "pressure preserves compiler tokens and health"
    )
    assert result["baseline"], "shutdown restores the isolated process baseline"
    assert result["descriptors"]["high_water"] <= result["descriptors"]["budget"], (
        "status exposes bounded high-water"
    )
