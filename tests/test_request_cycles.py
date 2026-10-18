"""Repeated requester faults distinguish bounded retention from descriptor leaks."""

import errno
import os
import socket
import typing as typ

import pytest
from descriptor_helpers import isolated, open_fds
from pool_helpers import attach_pool
from request_helpers import compile_request

from buildlimits import launch
from buildlimits.config import SupervisorConfig
from buildlimits.protocol import Queued
from buildlimits.supervisor import Supervisor

if typ.TYPE_CHECKING:
    from pathlib import Path


def _cycle(supervisor: Supervisor, mode: str) -> None:
    """Exercise real recorded sockets through cancellation or failed launch."""
    baseline = open_fds()
    client, server = socket.socketpair()
    source = os.dup(server.fileno())
    with client, server:
        entry = supervisor.requests._admit(server, compile_request((8,)), [source])
        assert entry is not None, "each cycle begins with recorded ownership"
        supervisor._apply(supervisor.admission.submit(entry.request_id))
        if mode == "spawn":
            supervisor._apply(supervisor.admission.token_arrived(b"+"))
        else:
            client.close()
            if mode == "reply":
                supervisor.requests.notify(entry.request_id, Queued(1))
                supervisor._failed_replies()
            else:
                supervisor._connection_ready(entry.request_id)
        assert (
            not supervisor.requests.pending and not supervisor.requests.failed_replies
        ), "every completed fault ends requester ownership"
        assert (
            not supervisor.admission.queue and supervisor.admission.tokens_held() == 0
        ), "no request or compiler token is stranded"
        assert supervisor.admission.healthy, (
            "requester faults do not damage admission health"
        )
        with pytest.raises(OSError, match="Bad file descriptor") as caught:
            os.fstat(source)
        assert caught.value.errno == errno.EBADF, (
            "the received right closes exactly once"
        )
    assert open_fds() == baseline, "each round recovers its exact descriptor baseline"


def _repeated(_report: int, path: Path) -> object:
    """Repeat three ownership outcomes twelve times in one disposable process."""
    baseline = open_fds()
    supervisor = Supervisor(SupervisorConfig(cache_mode="direct"))
    pool = attach_pool(supervisor)
    supervisor.groups.slice_dir = path
    try:
        with pytest.MonkeyPatch.context() as patch:

            def fail(_spec: launch.LaunchSpec) -> launch.HeldChild:
                """Refuse pipe allocation before any compiler process is created."""
                raise OSError(errno.EMFILE, "injected launch pressure")

            patch.setattr(launch, "spawn_held", fail)
            for _round in range(12):
                for mode in ("cancel", "reply", "spawn"):
                    _cycle(supervisor, mode)
        supervisor.requests.close_all()
        supervisor.requests.close_all()
        assert not supervisor.selector.get_map(), "shutdown retains no requester watch"
        result = {
            "returned": len(pool.returned),
            "held": supervisor.admission.tokens_held(),
            "retained": supervisor.requests.retained,
        }
    finally:
        supervisor.requests.close_all()
        supervisor.selector.close()
    result["baseline"] = open_fds() == baseline
    return result


def test_repeated_cancel_reply_and_spawn_failures_recover(tmp_path: Path) -> None:
    """Thirty-six faults restore baselines and return each admitted token once."""
    result = isolated(lambda report: _repeated(report, tmp_path))
    assert isinstance(result, dict), "the child reports fault-cycle evidence"
    assert result["returned"] == 12, "each admitted token returns exactly once"
    assert result["held"] == 0, "no completed compiler retains admission"
    assert result["retained"] == 0, "request ownership is fully reclaimed"
    assert result["baseline"], "all cycles restore the exact descriptor baseline"
