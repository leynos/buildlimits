"""Typed manager-transport faults and public user-bus recovery.

Injected command runners model private refusal and independently reachable public
transport. Run with ``uv run pytest -v
tests/test_manager_transport.py`` from the repository root.
"""

import json
import subprocess
import typing as typ

import pytest

from buildlimits.systemd import SystemdError, UserManager

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _reply(argv: cabc.Sequence[str], value: object) -> subprocess.CompletedProcess[str]:
    """Encode one typed bus response without invoking any external command."""
    return subprocess.CompletedProcess(list(argv), 0, json.dumps(value), "")


def _manager(
    properties: cabc.Mapping[str, object],
    calls: list[list[str]],
    unit_properties: cabc.Mapping[str, object] | None = None,
) -> UserManager:
    """Model a refused private socket and successful public service snapshot."""

    def runner(argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Serve fixed manager queries and record their transport selection."""
        calls.append(list(argv))
        if argv[0] == "systemctl":
            return subprocess.CompletedProcess(
                list(argv), 1, "", "Failed to connect: Connection refused"
            )
        if "GetUnit" in argv:
            return _reply(
                argv,
                {
                    "type": "o",
                    "data": ["/org/freedesktop/systemd1/unit/test_2eservice"],
                },
            )
        if "GetAll" in argv:
            selected = unit_properties if argv[-1].endswith(".Unit") else properties
            return _reply(argv, {"type": "a{sv}", "data": [selected or {}]})
        msg = "unexpected manager query"
        raise AssertionError(msg)

    return UserManager(runner)


def test_private_refusal_recovers_through_public_user_bus() -> None:
    """A reachable public manager bus supplies one validated property snapshot."""
    calls = []
    properties = {
        "MainPID": {"type": "u", "data": 4242},
        "ControlGroup": {"type": "s", "data": "/user.slice/cache.service"},
        "Environment": {"type": "as", "data": ["SECRET_TOKEN=must-not-be-emitted"]},
    }
    result = _manager(properties, calls).show(
        "cache.service", "MainPID", "ControlGroup"
    )
    assert result == {"MainPID": "4242", "ControlGroup": "/user.slice/cache.service"}, (
        "only requested load properties escape the manager port"
    )
    assert len(calls) == 3, (
        "one private query then object resolution and one public snapshot"
    )
    assert "--user" in calls[0], "the normal manager transport is attempted first"
    assert all(
        any(arg.startswith("--address=unix:path=/run/user/") for arg in call)
        for call in calls[1:]
    ), "fallback explicitly selects the owner public user bus"
    assert all("--timeout=3s" in call for call in calls[1:]), (
        "public queries have bounded timeouts"
    )


@pytest.mark.parametrize("pid", [True, -1, "4242", None])
def test_malformed_public_pid_cannot_become_idle(pid: object) -> None:
    """Typed bus responses with invalid process identifiers fail closed."""
    calls = []
    manager = _manager(
        {
            "MainPID": {"type": "u", "data": pid},
            "ControlGroup": {"type": "s", "data": "/user.slice/cache.service"},
        },
        calls,
    )
    with pytest.raises(SystemdError):
        manager.show("cache.service", "MainPID", "ControlGroup")
    assert len(calls) == 3, "malformed public data is checked rather than ignored"


def test_public_snapshot_omission_is_an_explicit_query_failure() -> None:
    """A live MainPID without its cgroup never establishes zero active work."""
    calls = []
    manager = _manager({"MainPID": {"type": "u", "data": 4242}}, calls)
    with pytest.raises(SystemdError):
        manager.show("cache.service", "MainPID", "ControlGroup")
    assert len(calls) == 3, "the missing property is a public snapshot fault"


def test_stopped_proof_reads_unit_state_separately_from_service_properties() -> None:
    """A service snapshot cannot substitute for the manager unit lifecycle state."""
    calls: list[list[str]] = []
    properties = {
        "MainPID": {"type": "u", "data": 0},
        "ControlGroup": {"type": "s", "data": ""},
        "ActiveState": {"type": "s", "data": "active"},
    }
    unit = {"ActiveState": {"type": "s", "data": "inactive"}}
    result = _manager(properties, calls, unit).show(
        "cache.service", "MainPID", "ControlGroup", "ActiveState"
    )
    assert result["MainPID"] == "0", "the service snapshot owns the PID"
    assert not result["ControlGroup"], "the service snapshot owns the cgroup"
    assert result["ActiveState"] == "inactive", "the later Unit snapshot owns lifecycle"
    assert len(calls) == 4, "private refusal precedes three bounded public calls"
    assert calls[-2][-1].endswith(".Service"), "PID and cgroup share one snapshot"
    assert calls[-1][-1].endswith(".Unit"), "unit state is checked after that snapshot"


@pytest.mark.parametrize(
    "state", [None, {"type": "u", "data": 0}, {"type": "s", "data": False}]
)
def test_invalid_unit_state_cannot_establish_a_stopped_proof(state: object) -> None:
    """Absent or wrongly typed Unit state remains an explicit manager fault."""
    calls: list[list[str]] = []
    service = {
        "MainPID": {"type": "u", "data": 0},
        "ControlGroup": {"type": "s", "data": ""},
        "ActiveState": {"type": "s", "data": "inactive"},
    }
    unit = {"ActiveState": state}
    with pytest.raises(SystemdError):
        _manager(service, calls, unit).show(
            "cache.service", "MainPID", "ControlGroup", "ActiveState"
        )
    assert len(calls) == 4, "invalid Unit state cannot fall back to Service state"
