"""Verified stopped-service accounting for cached drain tokens."""

import subprocess
import typing as typ
from pathlib import Path

import pytest
from cache_recovery_helpers import fixture_cgroup

from buildlimits import sccache
from buildlimits.proc_trees import count_children
from buildlimits.sccache import (
    ManagedSccache,
    SccacheError,
    StoppedPopulatedCacheError,
)
from buildlimits.systemd import UserManager

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _server(properties: dict[str, str]) -> ManagedSccache:
    """Provide fixed manager properties without invoking an external command."""

    def runner(argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Return only the specified service state."""
        text = "".join(f"{key}={value}\n" for key, value in properties.items())
        return subprocess.CompletedProcess(list(argv), 0, text, "")

    return ManagedSccache(
        "fixture.service", Path("/fixture/socket"), UserManager(runner)
    )


@pytest.mark.parametrize("state", ["inactive", "failed"])
def test_no_allocated_cgroup_and_stopped_state_account_for_old_work(state: str) -> None:
    """A manager-confirmed stop with no allocated cgroup admits no detached work."""
    server = _server({"MainPID": "0", "ControlGroup": "", "ActiveState": state})
    assert server.accounted_drain() == 0, "only a verified empty stop supplies zero"


@pytest.mark.parametrize(
    "state", ["active", "activating", "deactivating", "", "unknown"]
)
def test_zero_pid_without_stopped_state_does_not_account_for_work(state: str) -> None:
    """Transient unit states cannot stand in for an empty service stop."""
    server = _server({"MainPID": "0", "ControlGroup": "", "ActiveState": state})
    with pytest.raises(SccacheError, match="verified stopped"):
        server.accounted_drain()


@pytest.mark.parametrize("missing", ["MainPID", "ControlGroup", "ActiveState"])
def test_omitted_manager_properties_remain_unknown(missing: str) -> None:
    """An incomplete manager result cannot imply an empty cache service."""
    properties = {"MainPID": "0", "ControlGroup": "", "ActiveState": "inactive"}
    del properties[missing]
    with pytest.raises(SccacheError, match="omitted"):
        _server(properties).accounted_drain()


@pytest.mark.parametrize("path", ["relative/path", "/user.slice/../other"])
def test_invalid_cgroup_paths_cannot_account_for_detached_work(path: str) -> None:
    """Manager paths remain constrained to absolute non-traversing hierarchy paths."""
    server = _server({"MainPID": "0", "ControlGroup": path, "ActiveState": "inactive"})
    with pytest.raises(SccacheError, match="invalid"):
        server.accounted_drain()


def _stopped_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, procs: str
) -> ManagedSccache:
    """Report a stopped service whose control group lists ``procs``.

    Returns
    -------
    ManagedSccache
        The managed server over a stopped unit and a fixture control group.
    """
    fixture_cgroup(tmp_path, monkeypatch, procs)
    return _server({
        "MainPID": "0",
        "ControlGroup": "/fixture.service",
        "ActiveState": "inactive",
    })


def _proc_entry(proc: Path, pid: int, parent: int) -> None:
    """Write one fixture ``/proc/<pid>/stat`` naming the process's parent."""
    directory = proc / str(pid)
    directory.mkdir()
    (directory / "stat").write_text(f"{pid} (fixture) S {parent} 0", encoding="ascii")


@pytest.mark.parametrize("contents", ["", "4242\n"])
def test_stopped_unit_requires_empty_cgroup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str
) -> None:
    """Stopped manager state still requires all descendant processes to be gone."""
    server = _stopped_server(tmp_path, monkeypatch, contents)
    if contents:
        with pytest.raises(StoppedPopulatedCacheError, match="still contains work"):
            server.accounted_drain()
    else:
        assert server.accounted_drain() == 0, "stopped state and empty cgroup agree"


def test_a_stopped_but_populated_service_is_a_distinct_fault_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stopped service with surviving work is not a live-work or transport fault."""
    server = _stopped_server(tmp_path, monkeypatch, "4242\n")
    with pytest.raises(SccacheError) as caught:
        server.accounted_drain()
    assert isinstance(caught.value, StoppedPopulatedCacheError), (
        "the stopped-but-populated state is classified separately"
    )
    assert not isinstance(caught.value, sccache.LiveCacheWorkError), (
        "a stopped service is not live work"
    )


def test_live_server_with_zero_visible_children_retains_unknown_work() -> None:
    """A live cache may retain accepted requests waiting for jobserver tokens."""
    server = _server({"MainPID": "4242", "ControlGroup": "", "ActiveState": "active"})
    with pytest.raises(SccacheError, match="operator accounting"):
        server.accounted_drain()


def test_orphaned_compiler_roots_remain_visible_without_double_counting(
    tmp_path: Path,
) -> None:
    """Detached compiler trees count separately while their linkers share ownership."""
    for pid, parent in ((100, 1), (101, 100), (102, 1), (103, 102)):
        _proc_entry(tmp_path, pid, parent)
    assert count_children(100, [100, 101, 102, 103], proc=tmp_path) == 2, (
        "one direct compiler and one orphan tree remain active"
    )


def test_cyclic_process_snapshots_are_unknown(tmp_path: Path) -> None:
    """A malformed process tree cannot become a reassuring zero count."""
    for pid, parent in ((101, 102), (102, 101)):
        _proc_entry(tmp_path, pid, parent)
    with pytest.raises(ValueError, match="cyclic"):
        count_children(100, [101, 102], proc=tmp_path)


def test_live_idle_diagnostic_reuses_snapshot_without_proving_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero root count is diagnostic only and requires no second manager query."""
    fixture_cgroup(tmp_path, monkeypatch)
    calls: list[list[str]] = []

    def runner(argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Return one consistent live idle manager snapshot."""
        calls.append(list(argv))
        return subprocess.CompletedProcess(
            list(argv),
            0,
            "MainPID=4242\nControlGroup=/fixture.service\nActiveState=active\n",
            "",
        )

    server = ManagedSccache("fixture.service", tmp_path / "socket", UserManager(runner))
    with pytest.raises(SccacheError, match="visible compiler roots=0"):
        server.accounted_drain()
    assert len(calls) == 1, "root diagnostics reuse the same manager snapshot"
