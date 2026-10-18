"""Combined descriptor pressure, unhealthy drain and manager recovery.

A disposable process owns its sockets and one synthetic cached token. Scripted
manager transports and cgroup files distinguish reachable PID, live compilation
and measured idleness. Run with ``uv run pytest -v tests/test_combined_drain.py``.
"""

import dataclasses as dc
import json
import selectors
import socket
import subprocess
import typing as typ
from pathlib import Path

import pytest
from descriptor_helpers import fill_table, isolated, open_fds
from pool_helpers import attach_pool

from buildlimits import cgroups
from buildlimits.admission import Group, GroupState
from buildlimits.config import SupervisorConfig
from buildlimits.descriptors import close_all
from buildlimits.protocol import Exit
from buildlimits.requesters import listen
from buildlimits.status_render import render
from buildlimits.supervisor import Supervisor
from buildlimits.systemd import UserManager

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@dc.dataclass(slots=True)
class _FaultState:
    """Keep scripted transport, service and monotonic-clock changes together."""

    now: float = 0.0
    available: bool = False
    stopped: bool = False


def _manager(state: _FaultState, calls: list[list[str]]) -> UserManager:
    """Build scripted private/public transports without starting processes."""

    def runner(argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Refuse private transport and independently toggle the public bus."""
        calls.append(list(argv))
        if argv[0] == "systemctl" or not state.available:
            return subprocess.CompletedProcess(argv, 1, "", "Connection refused")
        if "GetUnit" in argv:
            value = {
                "type": "o",
                "data": ["/org/freedesktop/systemd1/unit/cache_2eservice"],
            }
        else:
            value = {
                "type": "a{sv}",
                "data": [
                    {
                        "MainPID": {"type": "u", "data": 0 if state.stopped else 4242},
                        "ControlGroup": {"type": "s", "data": "/cache.service"},
                        "ActiveState": {
                            "type": "s",
                            "data": "inactive" if state.stopped else "active",
                        },
                    }
                ],
            }
        return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")

    return UserManager(runner)


def _enter_unhealthy_drain(supervisor: Supervisor) -> None:
    """Model a cached group exiting by signal during an existing unhealthy drain."""
    supervisor.admission.groups["g1"] = Group(
        "g1", 1, b"+", True, state=GroupState.RUNNING
    )
    supervisor._unhealthy("injected pre-existing control fault")
    supervisor._apply(supervisor.admission.leader_exited("g1", Exit(signal=15)))
    supervisor._apply(supervisor.admission.group_emptied("g1"))


def _combined(_report: int, directory: str) -> object:
    """Hold detached cached work until an independently verified empty service stop."""
    path = Path(directory)
    state = _FaultState()
    supervisor = Supervisor(
        SupervisorConfig(cache_mode="sccache"),
        _manager(state, []),
        clock=lambda: state.now,
    )
    pool = attach_pool(supervisor)
    fillers = []
    with (
        supervisor.selector,
        listen(path / "supervisor.sock") as listener,
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client,
        pytest.MonkeyPatch.context() as patch,
    ):
        supervisor.listener = listener
        supervisor.selector.register(listener, selectors.EVENT_READ, "listener")
        patch.setattr(cgroups, "directory", lambda group: path / group.relative_to("/"))
        client.connect(listener.getsockname())
        baseline = open_fds()
        try:
            fillers = fill_table()
            healthy_under_pressure, pressure_state = _pressure_drain(supervisor, state)
            close_all(fillers)
            fillers.clear()
            held = _recover_cache(supervisor, path, state)
            result = {
                "pressure_errno": None,
                "healthy_under_pressure": healthy_under_pressure,
                **held,
                "returned": len(pool.returned),
                "exit": supervisor.exit_status,
                "status": pressure_state,
                "text": render(pressure_state),
                "baseline": open_fds() == baseline,
            }
        finally:
            close_all(fillers)
            for request_id in list(supervisor.requests.pending):
                supervisor.requests.forget(request_id)
    return result


def _poll_drain(supervisor: Supervisor, state: _FaultState) -> None:
    """Advance beyond the retry deadline before checking retained server tokens."""
    state.now += 31
    supervisor._apply(supervisor.server.drained_tokens())


def _pressure_drain(
    supervisor: Supervisor, state: _FaultState
) -> tuple[bool, dict[str, object]]:
    """Observe accept pressure separately from the independently unhealthy drain.

    Returns
    -------
    tuple[bool, dict[str, object]]
        Admission health under accept pressure and status under failed transport.
    """
    supervisor._accept()
    # The historic unhealthy drain is modelled independently: the new
    # accept-pressure policy must not itself make admission unhealthy.
    healthy_under_pressure = supervisor.admission.healthy
    _enter_unhealthy_drain(supervisor)
    for _ in range(10):
        _poll_drain(supervisor, state)
    return healthy_under_pressure, supervisor.status()


def _recover_cache(
    supervisor: Supervisor, path: Path, state: _FaultState
) -> dict[str, int]:
    """Sample token ownership through restored transport and verified cache stop.

    Returns
    -------
    dict[str, int]
        Held-token counts for unknown, active, live-idle and stopped work.
    """
    _poll_drain(supervisor, state)
    held = {"held_after_pressure": supervisor.admission.tokens_held()}
    # Reachable MainPID and one actual cgroup child still need a token.
    state.available = True
    _poll_drain(supervisor, state)
    held["held_while_active"] = supervisor.admission.tokens_held()
    (path / "cache.service" / "cgroup.procs").write_text("4242\n")
    _poll_drain(supervisor, state)
    held["held_while_live_idle"] = supervisor.admission.tokens_held()
    state.stopped = True
    (path / "cache.service" / "cgroup.procs").write_text("")
    _poll_drain(supervisor, state)
    supervisor._apply(supervisor.server.drained_tokens())
    held["held_after_idle"] = supervisor.admission.tokens_held()
    return held


def test_combined_pressure_and_transport_fault_recover_without_early_token_return(
    tmp_path: Path,
) -> None:
    """Only verified stopped cache work can release the retained drain token."""
    group = tmp_path / "cache.service"
    group.mkdir()
    (group / "cgroup.procs").write_text("4242\n4243\n")
    result = isolated(lambda report: _combined(report, str(tmp_path)))
    assert isinstance(result, dict), "the child reports combined state evidence"
    assert result["held_while_live_idle"] == 1, (
        "reachable live-idle service still cannot account for detached requests"
    )
    assert result["pressure_errno"] is None and result["healthy_under_pressure"], result
    assert result["held_after_pressure"] == result["held_while_active"] == 1, (
        "unknown and known live work both retain the token"
    )
    assert (
        result["held_after_idle"] == 0
        and result["returned"] == 1
        and result["exit"] == 1
    ), "verified stopped state releases exactly once before unhealthy exit"
    assert result["baseline"], "all pressure descriptors recover to baseline"
    status = result["status"]
    assert (
        status.get("recovery_action")
        and status.get("drain_fault")
        and status.get("health_reason")
    ), "structured status names fault and operator recovery"
    assert "None in None, Nones" not in result["text"], (
        "plain status explains missing drain metadata"
    )

    assert "drain check failed; tokens retained:" in result["text"], (
        "plain output names the failed cache accounting check"
    )
    assert "required recovery action:" in result["text"], (
        "plain output requires explicit operator recovery"
    )
    assert "request metadata unavailable; retaining compiler token" in result["text"], (
        "missing metadata is explained without hiding token ownership"
    )
