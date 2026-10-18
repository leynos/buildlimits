"""Tests for how a compiler that cannot start reaches its requester."""

import errno
import os
import selectors
import socket
import subprocess
import typing as typ
from pathlib import Path

import pytest
from launch_helpers import wait_reaped
from request_helpers import refusal_from

from buildlimits import attributes, launch
from buildlimits.attributes import ProcessAttributes
from buildlimits.config import SupervisorConfig
from buildlimits.effects import Exit
from buildlimits.groups import Running
from buildlimits.protocol import CompileRequest
from buildlimits.requesters import Pending
from buildlimits.supervisor import Supervisor
from buildlimits.systemd import UserManager

if typ.TYPE_CHECKING:
    import collections.abc as cabc

MISSING = "/nonexistent/build-limits-test-compiler"


def _quiet_runner(argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Answer every user-manager query successfully and with nothing."""
    return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def supervisor() -> cabc.Iterator[Supervisor]:
    """Build a supervisor that has not started, closing its selector after."""
    instance = Supervisor(
        SupervisorConfig(), UserManager(_quiet_runner), clock=lambda: 0.0
    )
    yield instance
    instance.selector.close()


def _released_missing_compiler(tmp_path: Path) -> launch.HeldChild:
    """Start, then release, a launch child whose compiler does not exist."""
    plan = attributes.AttributePlan(
        umask=0o22, nice=os.getpriority(os.PRIO_PROCESS, 0), rlimits={}
    )
    spec = launch.LaunchSpec(
        argv=(MISSING,),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        cwd=str(tmp_path),
        received_fds=(),
        fd_targets=(),
        plan=plan,
    )
    child = launch.spawn_held(spec)
    child.release()
    return child


def _pending(conn: socket.socket, tmp_path: Path) -> Pending:
    """Record an admitted request whose group is ``g1``."""
    request = CompileRequest(
        argv=(MISSING,),
        cwd=str(tmp_path),
        env={},
        attributes=ProcessAttributes(umask=0o22, nice=0, rlimits={}),
        fd_targets=(),
        label="widget",
        cacheable=False,
    )
    plan = attributes.AttributePlan(umask=0o22, nice=0, rlimits={})
    entry = Pending(1, conn, request, [], plan, arrived=0.0)
    entry.group_id = "g1"
    entry.admitted = 0.0
    return entry


def test_a_compiler_that_cannot_start_is_refused_as_exec_failed(
    supervisor: Supervisor,
    socket_pair: tuple[socket.socket, socket.socket],
    tmp_path: Path,
    telemetry,
) -> None:
    """The child's exec error travels through the groups to the requester."""
    metrics, metric_sink = telemetry
    supervisor.requests.metrics = metrics
    client, server = socket_pair
    child = _released_missing_compiler(tmp_path)
    try:
        assert child.errors is not None, "a released child keeps its error pipe"
        supervisor.selector.register(child.errors, selectors.EVENT_READ, "errors")
        supervisor.groups.running["g1"] = Running("g1", 1, tmp_path, child)
        supervisor.requests.pending[1] = _pending(server, tmp_path)
        status = wait_reaped(child, timeout=2.0)

        supervisor._dispatch(("errors", "g1"))
        supervisor.requests.finish(1, Exit(*launch.exit_of(status)))
    finally:
        child.close()

    reply = refusal_from(client)
    assert reply.reason == "exec-failed", "the refusal names the exec failure"
    assert Path(MISSING).name in reply.message, "the bounded display names the compiler"
    assert "stage=exec" in reply.message, "the refusal names the failing setup stage"
    assert f"errno={errno.ENOENT}" in reply.message, (
        "the numeric errno identifies the missing executable"
    )
    assert 1 not in supervisor.requests.pending, "the request is forgotten"

    assert len(metric_sink.records) == 1, "exec failure finishes once"
    assert metric_sink.records[0]["BUILD_LIMITS_OUTCOME"] == "exec_failed", (
        "exec failure classified"
    )
    assert MISSING not in str(metric_sink.records), (
        "compiler path never reaches metrics"
    )
