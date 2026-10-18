"""Tests for the operating-system side of admitted groups."""

import selectors
import typing as typ
from pathlib import Path

import pytest

from buildlimits import attributes, launch
from buildlimits.attributes import ProcessAttributes
from buildlimits.config import SupervisorConfig
from buildlimits.groups import Groups
from buildlimits.protocol import CompileRequest
from buildlimits.requesters import Pending
from buildlimits.systemd import SystemdError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.systemd import UserManager


@pytest.fixture
def selector() -> cabc.Iterator[selectors.BaseSelector]:
    """Close each test selector deterministically before its descriptors recycle."""
    with selectors.DefaultSelector() as owned:
        yield owned


def _entry(*, cacheable: bool) -> Pending:
    """Build an admitted request for ``rustc``."""
    request = CompileRequest(
        argv=("/bin/sccache", "rustc", "-vV"),
        cwd="/",
        env={},
        attributes=ProcessAttributes(umask=0o22, nice=0, rlimits={}),
        fd_targets=(),
        label="rustc",
        cacheable=cacheable,
    )
    plan = attributes.AttributePlan(umask=0o22, nice=0, rlimits={})
    return Pending(1, None, request, [], plan, arrived=0.0)


def _groups(selector: selectors.BaseSelector, cache_mode: str = "sccache") -> Groups:
    """Build groups with a configuration and no user manager."""
    settings = SupervisorConfig(
        cache_mode=cache_mode,
        sccache_executable="/managed/sccache",
        sccache_socket=Path("/run/user/1/buildlimits/sccache.sock"),
    )
    return Groups(
        settings,
        typ.cast("UserManager", object()),
        selector,
        lambda: 0.0,
    )


def test_an_unavailable_cache_is_announced(selector: selectors.BaseSelector) -> None:
    """A build meant to be cached is told when it compiles without the cache."""
    notice = _groups(selector).cache_notice(_entry(cacheable=True), cached=False)
    assert "compiling without the cache" in notice, notice


@pytest.mark.parametrize(
    ("cache_mode", "cacheable", "cached"),
    [("sccache", True, True), ("sccache", False, False), ("direct", True, False)],
)
def test_intended_composition_is_not_announced(
    selector: selectors.BaseSelector, cache_mode: str, cacheable: bool, cached: bool
) -> None:
    """Cached groups, rustdoc and configured direct mode say nothing."""
    notice = _groups(selector, cache_mode).cache_notice(
        _entry(cacheable=cacheable), cached=cached
    )
    assert not notice, notice


def test_a_cached_launch_uses_the_managed_server(
    selector: selectors.BaseSelector,
) -> None:
    """A cached group runs the managed client against the private socket."""
    spec = _groups(selector).launch_spec(_entry(cacheable=True), cached=True)
    assert spec.argv == ("/managed/sccache", "rustc", "-vV"), spec.argv
    assert spec.env["SCCACHE_SERVER_UDS"] == "/run/user/1/buildlimits/sccache.sock", (
        "the client is pointed at the managed server"
    )
    assert spec.env["SCCACHE_IGNORE_SERVER_IO_ERROR"] == "1", (
        "losing the server falls back to compiling in the scope"
    )


def test_an_uncached_launch_drops_the_callers_sccache_layer(
    selector: selectors.BaseSelector,
) -> None:
    """An uncached group runs the bare compiler, never the caller's client."""
    spec = _groups(selector).launch_spec(_entry(cacheable=True), cached=False)
    assert spec.argv == ("rustc", "-vV"), spec.argv
    assert "SCCACHE_SERVER_UDS" not in spec.env, "no server is named"


class _Child:
    """A held launch child that records whether it was abandoned."""

    pid = 4242

    def __init__(self) -> None:
        """Start as a live, held child."""
        self.abandoned = False

    def abandon(self) -> None:
        """Record the abandonment."""
        self.abandoned = True


class _BrokenManager:
    """A user manager whose scope creation fails with a given error."""

    def __init__(self, error: Exception) -> None:
        """Remember the error to raise."""
        self.error = error

    def start_scope(self, unit: str, pid: int, slice_unit: str) -> None:
        """Fail to create the scope."""
        del unit, pid, slice_unit
        raise self.error


def _placing_groups(selector: selectors.BaseSelector, manager: object) -> Groups:
    """Build groups that have passed the restart barrier."""
    groups = Groups(
        SupervisorConfig(),
        typ.cast("UserManager", manager),
        selector,
        lambda: 0.0,
    )
    groups.slice_dir = Path("/sys/fs/cgroup/compilers.slice")
    return groups


def test_a_child_that_cannot_start_fails_the_launch(
    monkeypatch: pytest.MonkeyPatch,
    selector: selectors.BaseSelector,
) -> None:
    """Resource exhaustion at fork fails the launch instead of escaping."""

    def exhausted(_spec: object) -> None:
        """Fail as a fork does when the process limit is reached."""
        raise OSError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(launch, "spawn_held", exhausted)
    groups = _placing_groups(selector, object())
    assert groups.place(_entry(cacheable=False), "g1", cached=False) is None, (
        "a launch child that cannot start fails the launch"
    )
    assert not groups.running, "a failed launch is not tracked as running"


@pytest.mark.parametrize(
    "error",
    [OSError(24, "Too many open files"), SystemdError("cannot create the scope")],
    ids=["os-error", "systemd-error"],
)
def test_a_scope_that_cannot_be_made_abandons_the_child(
    monkeypatch: pytest.MonkeyPatch, selector: selectors.BaseSelector, error: Exception
) -> None:
    """Any failure to place the child in its scope abandons it."""
    child = _Child()
    monkeypatch.setattr(launch, "spawn_held", lambda _spec: child)
    groups = _placing_groups(selector, _BrokenManager(error))
    assert groups.place(_entry(cacheable=False), "g1", cached=False) is None, (
        "a child outside its scope fails the launch"
    )
    assert child.abandoned, "the held child is abandoned"
    assert not groups.running, "a failed launch is not tracked as running"
