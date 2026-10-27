"""A graceful stop lets running groups finish; only an immediate stop cancels them.

``systemctl stop`` and ``restart`` send ``SIGTERM``, so the supervisor must not
cancel an admitted compile or refuse a waiting build for it. These tests drive
the shell the signal handlers drive: the handler records, the next tick acts.
"""

import dataclasses as dc
import typing as typ

import pytest
from cache_unit_fake import STOPPED, CacheUnit
from pool_helpers import attach_pool
from request_helpers import crate_pending

from buildlimits import kache_guard
from buildlimits.admission import Group, GroupState
from buildlimits.cache_backend import CacheBackend, DirectCache
from buildlimits.config import SupervisorConfig
from buildlimits.effects import Exit
from buildlimits.pool import Token
from buildlimits.supervisor import Supervisor
from buildlimits.supervisor_shutdown import settle_token_request
from buildlimits.systemd import UserManager

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.groups import Groups


@dc.dataclass(slots=True)
class RecordingGroups:
    """A groups double that records which groups were terminated.

    The status path asks the registry which configuration and which backend it
    stands for, to decide whether a stray cache process could exist at all.
    The defaults answer ``direct`` with an in-scope backend, so the guard stays
    off; the fixture replaces both with the supervisor's own, so the double
    keeps reporting the configuration it is standing in for.
    """

    terminated: list[str] = dc.field(default_factory=list)
    running: dict[str, object] = dc.field(default_factory=dict)
    settings: SupervisorConfig = dc.field(
        default_factory=lambda: SupervisorConfig(cache_mode="direct")
    )
    backend: CacheBackend = dc.field(default_factory=DirectCache)
    slice_dir: Path | None = None
    guard: kache_guard.Strays = dc.field(default_factory=kache_guard.Strays)
    terminate: cabc.Callable[[str], None] = dc.field(init=False)

    def __post_init__(self) -> None:
        """Record a termination instead of signalling a scope."""
        self.terminate = self.terminated.append

    def advance(self, draining: object) -> list[str]:
        """Report no group as emptied."""
        del draining
        return []

    def remove(self, group_id: str) -> None:
        """Forget nothing: the scripted groups own no scope."""
        del group_id


@pytest.fixture
def running(tmp_path: Path) -> cabc.Iterator[tuple[Supervisor, RecordingGroups]]:
    """Build a supervisor with one running group (g1) and one waiting request (2)."""
    unit = CacheUnit(barrier=tmp_path / "cache-recovery", state=STOPPED)
    supervisor = Supervisor(
        SupervisorConfig(recovery_marker=unit.barrier),
        UserManager(unit.runner),
        clock=lambda: -1.0,
    )
    groups = RecordingGroups(settings=supervisor.settings, backend=supervisor.backend)
    supervisor.groups = typ.cast("Groups", groups)
    attach_pool(supervisor)
    supervisor.admission.groups["g1"] = Group("g1", 1, b"+", cached=False)
    supervisor.requests.pending[1] = crate_pending(1, "g1")
    supervisor.admission.submit(2)
    supervisor.requests.pending[2] = crate_pending(2)
    yield supervisor, groups
    supervisor.selector.close()


def test_a_graceful_stop_waits_for_the_running_group_and_cancels_nothing(
    running: tuple[Supervisor, RecordingGroups],
) -> None:
    """The tick that honours SIGTERM terminates nothing and does not exit."""
    supervisor, groups = running
    supervisor.request_graceful_stop()
    supervisor._tick()
    assert supervisor.admission.closing, "the stop was honoured on the tick"
    assert not groups.terminated, "the admitted compile was not cancelled"
    assert supervisor.admission.groups["g1"].state is GroupState.RUNNING, (
        "the group still runs"
    )
    assert supervisor.exit_status is None, "the supervisor outlives its running group"
    assert list(supervisor.admission.queue) == [2], "the waiting build keeps waiting"
    assert supervisor.status()["restarting"] is True, "the status says why it waits"


def test_a_graceful_stop_exits_cleanly_once_the_last_group_has_emptied(
    running: tuple[Supervisor, RecordingGroups],
) -> None:
    """The last group's token returns, the queue is refused, and the exit is 0."""
    supervisor, _groups = running
    pool = typ.cast("typ.Any", supervisor.pool)
    supervisor.request_graceful_stop()
    supervisor._tick()
    supervisor._apply(supervisor.admission.leader_exited("g1", Exit(code=0)))
    supervisor._apply(supervisor.admission.group_emptied("g1"))
    assert pool.returned == [b"+"], "the finished group's token went back"
    assert 2 not in supervisor.requests.pending, "the waiting request was refused"
    assert supervisor.exit_status == 0, "a graceful stop is an orderly exit"


def test_an_immediate_stop_still_cancels_after_a_graceful_one(
    running: tuple[Supervisor, RecordingGroups],
) -> None:
    """SIGINT is the escape hatch when waiting for the group is not acceptable."""
    supervisor, groups = running
    supervisor.request_graceful_stop()
    supervisor._tick()
    supervisor.request_stop()
    supervisor._tick()
    assert groups.terminated == ["g1"], "the immediate stop cancelled the group"
    assert not supervisor.admission.healthy, "and refused the queue"
    assert supervisor.status()["restarting"] is False, (
        "an immediate stop is not reported as waiting for the groups it cancels"
    )


def test_a_repeated_graceful_stop_changes_nothing(
    running: tuple[Supervisor, RecordingGroups],
) -> None:
    """A second SIGTERM neither terminates nor logs a second stop."""
    supervisor, groups = running
    for _ in range(3):
        supervisor.request_graceful_stop()
        supervisor._tick()
    assert not groups.terminated, "repeating the request cancels nothing"
    assert supervisor.exit_status is None, "and does not exit early"


def test_only_server_drain_groups_open_the_recovery_that_releases_them(
    running: tuple[Supervisor, RecordingGroups],
) -> None:
    """Cached tokens that await accounting are released by stopping the cache."""
    supervisor, _groups = running
    supervisor.request_graceful_stop()
    supervisor._tick()
    assert supervisor.recovery.describe(0.0)["phase"] == "idle", (
        "a running group is waited for, not recovered around"
    )
    supervisor.admission.groups["g1"].state = GroupState.SERVER_DRAIN
    supervisor._tick()
    assert supervisor.recovery.describe(0.0)["phase"] == "quiescing", (
        "a server-drain-only supervisor opens the transaction that frees its tokens"
    )


def test_the_status_text_says_the_supervisor_is_restarting(
    running: tuple[Supervisor, RecordingGroups],
) -> None:
    """An operator watching a restart sees why the old supervisor lingers."""
    from buildlimits.status_render import render

    supervisor, _groups = running
    supervisor.request_graceful_stop()
    supervisor._tick()
    lines = render(supervisor.status()).splitlines()
    assert lines[0] == "admission closing: supervisor is closing", (
        "the admission state leads the restart diagnostic"
    )
    assert "healthy, restarting once running groups finish" in lines[1], (
        "the summary says that the supervisor waits for its running groups"
    )


def test_a_token_read_for_a_request_nobody_launches_goes_back_before_exit(
    running: tuple[Supervisor, RecordingGroups],
) -> None:
    """The pool reader's in-flight token is returned, not left to the device."""
    supervisor, _groups = running
    pool = typ.cast("typ.Any", supervisor.pool)
    supervisor.request_graceful_stop()
    supervisor._tick()
    assert supervisor.admission.token_requested, "a reader waits for a token"
    pool.scripted.append(Token(b"-"))
    settle_token_request(supervisor)
    assert pool.returned == [b"-"], "the reader's token went back to the pool"
    assert not supervisor.admission.token_requested, "nothing is left in flight"


def test_a_token_ready_in_the_same_cycle_as_the_signal_launches_nothing(
    running: tuple[Supervisor, RecordingGroups],
) -> None:
    """The stop is honoured before a ready descriptor is dispatched, not after."""
    supervisor, _groups = running
    pool = typ.cast("typ.Any", supervisor.pool)
    pool.scripted.append(Token(b"-"))
    supervisor.request_graceful_stop()
    supervisor._dispatch("pool")
    assert pool.returned == [b"-"], "the token went straight back to the pool"
    assert list(supervisor.admission.groups) == ["g1"], "no group was launched"
    assert list(supervisor.admission.queue) == [2], "the waiting build still waits"
