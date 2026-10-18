"""Tests for control-group helpers and supervisor start-up."""

import itertools
import os
import signal
from pathlib import Path, PurePosixPath

import pytest
from hypothesis import given
from hypothesis import strategies as st

from buildlimits import cgroups, startup
from buildlimits.timing import Clock

SLICE = "buildlimits-compilers.slice"
_names = st.text(st.characters(categories=["L", "N"]), min_size=1, max_size=8)


@given(st.lists(_names, max_size=5))
def test_unified_paths_round_trip(parts: list[str]) -> None:
    """A unified-hierarchy line yields its path; others are ignored."""
    path = "/" + "/".join(parts)
    text = f"1:name=systemd:/legacy\n0::{path}\n"
    assert cgroups.parse_unified_path(text) == PurePosixPath(path), (
        "the unified-hierarchy line's path round-trips"
    )


@given(st.lists(_names, max_size=4), _names)
def test_only_scopes_directly_under_the_slice_count(
    prefix: list[str], name: str
) -> None:
    """A scope counts only when its parent is the compilers slice."""
    scope = f"{cgroups.SCOPE_PREFIX}{name}{cgroups.SCOPE_SUFFIX}"
    base = PurePosixPath("/", *prefix)
    assert cgroups.is_admitted_scope(base / SLICE / scope, SLICE), (
        "a scope directly under the slice is admitted"
    )
    assert cgroups.is_admitted_scope(base / SLICE / scope / "child", SLICE), (
        "a descendant of an admitted scope is admitted"
    )
    assert not cgroups.is_admitted_scope(base / scope, SLICE), (
        "a scope outside the slice is not admitted"
    )
    assert not cgroups.is_admitted_scope(base / SLICE / f"{name}.scope", SLICE), (
        "a scope missing the compiler prefix is not admitted"
    )


def _fake_group(root: Path, name: str, *, populated: bool, pids: list[int]) -> Path:
    """Create a directory that looks like a control group."""
    group = root / name
    group.mkdir(parents=True)
    (group / "cgroup.events").write_text(f"populated {int(populated)}\nfrozen 0\n")
    (group / "cgroup.procs").write_text("".join(f"{pid}\n" for pid in pids))
    return group


def test_population_and_members_are_read(tmp_path: Path) -> None:
    """Population comes from cgroup.events; members from every cgroup.procs."""
    group = _fake_group(tmp_path, "a.scope", populated=True, pids=[10])
    _fake_group(group, "sub", populated=True, pids=[11, 12])
    assert cgroups.is_populated(group), "the group with a member is populated"
    assert sorted(cgroups.member_pids(group)) == [
        10,
        11,
        12,
    ], "members come from every nested cgroup.procs"
    assert not cgroups.is_populated(tmp_path / "gone.scope"), (
        "an absent group is not populated"
    )
    assert not cgroups.member_pids(tmp_path / "gone.scope"), (
        "an absent group has no members"
    )


def test_an_unreadable_group_is_not_taken_for_empty(tmp_path: Path) -> None:
    """Only a removed group reads as empty; any other read failure is raised."""
    group = tmp_path / "a.scope"
    # A directory where cgroup.events belongs fails to read with EISDIR.
    (group / "cgroup.events").mkdir(parents=True)
    with pytest.raises(IsADirectoryError):
        cgroups.is_populated(group)


def test_an_unreadable_member_list_is_not_taken_for_empty(tmp_path: Path) -> None:
    """A member list that cannot be read is raised, not counted as no members."""
    group = _fake_group(tmp_path, "a.scope", populated=True, pids=[10])
    nested = group / "sub"
    nested.mkdir()
    # A self-referencing link fails to read with ELOOP, even for root.
    (nested / "cgroup.procs").symlink_to("cgroup.procs")
    with pytest.raises(OSError, match="symbolic links"):
        cgroups.member_pids(group)


def test_forced_termination_writes_cgroup_kill(tmp_path: Path) -> None:
    """SIGKILL goes through cgroup.kill; an absent group is ignored."""
    group = _fake_group(tmp_path, "a.scope", populated=True, pids=[])
    cgroups.terminate(group, force=True)
    assert (group / "cgroup.kill").read_text() == "1", (
        "forced termination writes cgroup.kill"
    )
    cgroups.terminate(tmp_path / "gone.scope", force=True)


def test_forced_termination_kills_members_without_cgroup_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kernel without cgroup.kill still has every member killed."""
    group = _fake_group(tmp_path, "a.scope", populated=True, pids=[10, 11])
    # A directory in its place makes the write fail as an absent file would.
    (group / "cgroup.kill").mkdir()
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        cgroups.os, "kill", lambda pid, number: sent.append((pid, number))
    )
    cgroups.terminate(group, force=True)
    assert sent == [(10, signal.SIGKILL), (11, signal.SIGKILL)], (
        "each member is sent SIGKILL directly"
    )


def test_membership_waits_until_the_process_moves(tmp_path: Path) -> None:
    """The barrier opens only once /proc shows the new scope."""
    clock = itertools.count(0.0, 0.5)
    own = cgroups.own_path()
    assert own is not None, "the test process is on the unified hierarchy"
    assert cgroups.wait_for_membership(
        os.getpid(),
        own.name,
        1.0,
        clock=Clock(now=lambda: next(clock), sleep=lambda _s: None),
    ), "the barrier opens once the process shows in its own scope"
    clock = itertools.count(0.0, 0.5)
    assert not cgroups.wait_for_membership(
        os.getpid(),
        "never.scope",
        1.0,
        clock=Clock(now=lambda: next(clock), sleep=lambda _s: None),
    ), "the barrier never opens for a scope the process never joins"


def test_child_scopes_lists_only_supervisor_scopes(tmp_path: Path) -> None:
    """Start-up drains supervisor scopes only."""
    _fake_group(tmp_path, "buildlimits-1-g1.scope", populated=False, pids=[])
    _fake_group(tmp_path, "other.scope", populated=False, pids=[])
    assert [p.name for p in cgroups.child_scopes(tmp_path)] == [
        "buildlimits-1-g1.scope"
    ], "only supervisor scopes are listed"
    assert cgroups.child_scopes(tmp_path / "absent.slice") == [], (
        "an absent slice has no child scopes"
    )


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("buildlimits.slice", "buildlimits.slice"),
        (
            "buildlimits-compilers.slice",
            "buildlimits.slice/buildlimits-compilers.slice",
        ),
        ("a-b-c.slice", "a.slice/a-b.slice/a-b-c.slice"),
    ],
)
def test_slice_paths_follow_dash_nesting(unit: str, expected: str) -> None:
    """Slice names nest at each dash, as systemd does."""
    assert startup.slice_path(PurePosixPath("/u"), unit) == PurePosixPath(
        "/u", expected
    ), "slice names nest at each dash"


def test_the_supervisor_must_run_under_a_user_manager() -> None:
    """Outside user@.service there is no subtree to create scopes in."""
    with pytest.raises(startup.StartupError, match="user service"):
        startup.user_manager_root(PurePosixPath("/init.scope"))


def test_drain_kills_survivors_and_waits(tmp_path: Path) -> None:
    """Surviving scopes are killed; start-up proceeds once they empty."""
    scope = _fake_group(tmp_path, "buildlimits-9-g1.scope", populated=True, pids=[])
    checks = iter([True, False])

    def empties(_seconds: float) -> None:
        """Simulate the scope emptying after the first sleep call."""
        if not next(checks):
            (scope / "cgroup.events").write_text("populated 0\n")

    assert startup.drain_slice(tmp_path, 5.0, Clock(sleep=empties)) == 1, (
        "the surviving scope is killed"
    )
    assert (scope / "cgroup.kill").read_text() == "1", (
        "start-up proceeds once the scope empties"
    )


def test_drain_refuses_to_start_past_the_timeout(tmp_path: Path) -> None:
    """A scope that will not drain prevents start-up."""
    _fake_group(tmp_path, "buildlimits-9-g1.scope", populated=True, pids=[])
    clock = itertools.count(0.0, 1.0)
    with pytest.raises(startup.StartupError, match="did not drain"):
        startup.drain_slice(
            tmp_path, 2.0, Clock(now=lambda: next(clock), sleep=lambda _s: None)
        )


def test_the_singleton_lock_excludes_a_second_supervisor(tmp_path: Path) -> None:
    """A second supervisor cannot take the lock."""
    lock = tmp_path / "run" / "supervisor.lock"
    held = startup.acquire_lock(lock)
    try:
        assert oct(lock.parent.stat().st_mode & 0o777) == oct(0o700), (
            "the lock directory is private to its owner"
        )
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            try:
                startup.acquire_lock(lock)
            except startup.StartupError:
                os._exit(0)
            os._exit(1)
        _pid, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0, (
            "the second supervisor's lock attempt is refused"
        )
    finally:
        os.close(held)
