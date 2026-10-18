"""A live managed cache at start-up is stopped under the barrier, not refused.

Incident: a plain ``systemctl restart`` of the supervisor left the managed
cache live, and the new supervisor refused it with exit 2 because a live server
may retain detached requests. Start-up has already terminated every admitted
scope by then, so nothing admitted can still be using the cache: the safe answer
is to stop it under the restart barrier and prove it stopped and empty before
the pool opens, which is what the recovery transaction does mid-run.
"""

import os
import signal
import typing as typ
from contextlib import nullcontext
from pathlib import PurePosixPath

import pytest
from cache_unit_fake import STOPPED, CacheUnit, FakeClock
from hypothesis import given, settings
from hypothesis import strategies as st
from pool_helpers import FakePool

from buildlimits import cgroups, startup
from buildlimits import supervisor as supervisor_module
from buildlimits.admission import Admission
from buildlimits.cache_quiesce import (
    QuiesceTimeoutError,
    quiesce_and_report,
    quiesce_live_cache,
)
from buildlimits.config import SupervisorConfig
from buildlimits.metrics import Metrics
from buildlimits.recovery_barrier import RecoveryBarrier
from buildlimits.sccache import ManagedSccache, SccacheError, StartupAccountingError
from buildlimits.server_watch import ServerWatch
from buildlimits.supervisor import Supervisor
from buildlimits.systemd import UserManager

if typ.TYPE_CHECKING:
    from pathlib import Path

    from conftest import RecordingSink

OWN_CGROUP = PurePosixPath(
    "/user.slice/user-1000.slice/user@1000.service/app.slice/s.service"
)


def _supervisor(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unit: CacheUnit,
) -> tuple[Supervisor, list[list[str]]]:
    """Build a supervisor over the scripted unit, recording when the pool opens."""
    clock = FakeClock()
    monkeypatch.setattr(startup, "drain_slice", lambda *_args: 0)
    monkeypatch.setattr(cgroups, "directory", lambda path: tmp_path / str(path)[1:])
    opened: list[list[str]] = []

    def pool(path: Path) -> FakePool:
        """Record the cache jobs issued at the moment admission opens."""
        del path
        opened.append(list(unit.jobs))
        read_end, write_end = os.pipe2(os.O_CLOEXEC)
        request.addfinalizer(lambda: (os.close(read_end), os.close(write_end)))
        return FakePool(wake_fd=read_end)

    monkeypatch.setattr(supervisor_module, "TokenPool", pool)
    settings = SupervisorConfig(
        cache_mode="sccache",
        socket_path=tmp_path / "s.sock",
        recovery_marker=unit.barrier,
    )
    instance = Supervisor(settings, UserManager(unit.runner), clock.timing())
    return instance, opened


def _close(instance: Supervisor) -> None:
    """Release everything a started supervisor opened, including its signal pipe.

    ``start`` installs a wake-up descriptor for ``SIGCHLD``. Leaving its pipe
    and handler behind would shift every later descriptor-baseline test in the
    same worker, so both ends are closed and the handler is restored here.
    """
    if instance.listener is not None:
        instance.listener.close()
    instance.selector.close()
    if instance._signals_read >= 0:
        write_end = signal.set_wakeup_fd(-1)
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        os.close(instance._signals_read)
        if write_end >= 0:
            os.close(write_end)


def test_a_live_idle_cache_is_stopped_under_the_barrier_then_restored(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start-up proves the live cache stopped and empty before the pool opens."""
    unit = CacheUnit(barrier=tmp_path / "run" / "cache-recovery")
    instance, opened = _supervisor(request, tmp_path, monkeypatch, unit)
    try:
        instance.start(OWN_CGROUP)
        assert unit.jobs == ["stop", "start"], "stopped once, then restored once"
        assert unit.barrier_at_job == {"stop": True, "start": False}, (
            "the barrier is written before the stop and cleared before the start"
        )
        assert opened == [["stop", "start"]], (
            "admission opened only after the cache was stopped and restored"
        )
        assert not unit.barrier.exists(), "no barrier outlives a clean start-up"
    finally:
        _close(instance)


def test_a_stopped_cache_is_left_alone(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache already stopped and empty needs no stop and writes no barrier."""
    unit = CacheUnit(barrier=tmp_path / "run" / "cache-recovery", state=STOPPED)
    instance, _opened = _supervisor(request, tmp_path, monkeypatch, unit)
    try:
        instance.start(OWN_CGROUP)
        assert not unit.jobs, "nothing to stop and no barrier to retire"
    finally:
        _close(instance)


@pytest.mark.parametrize("fault", ["refused-stop", "unwritable-barrier"])
def test_a_stop_that_cannot_be_queued_under_the_barrier_fails_closed(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    """Without the barrier, or with the stop refused, start-up opens no pool.

    The stop is never queued without the barrier that keeps the cache down, and
    a cache that was not stopped is never started.
    """
    blocked = tmp_path / "run"
    if fault == "unwritable-barrier":
        blocked.write_text("a file where the barrier's directory should be")
    unit = CacheUnit(
        barrier=blocked / "cache-recovery", refuses_stop=fault == "refused-stop"
    )
    instance, opened = _supervisor(request, tmp_path, monkeypatch, unit)
    try:
        with pytest.raises(StartupAccountingError, match="cannot stop the live"):
            instance.start(OWN_CGROUP)
        assert not opened, "no pool opens while the cache is unaccounted"
        assert "start" not in unit.jobs, "an unaccounted cache is never restarted"
        assert unit.jobs == (["stop"] if fault == "refused-stop" else []), (
            "a stop is queued only once the barrier is in place"
        )
    finally:
        _close(instance)


@pytest.mark.parametrize(
    "state",
    [
        "MainPID=0\nControlGroup=/missing/sccache\nActiveState=inactive\n",
        "MainPID=oops\nControlGroup=\nActiveState=active\n",
    ],
    ids=["stopped-but-populated", "unreadable-state"],
)
def test_unreadable_or_populated_cache_state_is_not_quiesced(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    """Only a cache the manager reports live is stopped; anything else refuses."""
    unit = CacheUnit(barrier=tmp_path / "run" / "cache-recovery", state=state)
    survivor = tmp_path / "missing" / "sccache"
    survivor.mkdir(parents=True)
    (survivor / "cgroup.procs").write_text("4343\n", encoding="ascii")
    instance, opened = _supervisor(request, tmp_path, monkeypatch, unit)
    try:
        with pytest.raises(SccacheError):
            instance.start(OWN_CGROUP)
        assert not unit.jobs, "a state that cannot be read is never acted on"
        assert not opened, "no pool opens on an unreadable cache"
    finally:
        _close(instance)


@settings(max_examples=40, deadline=None)
@given(
    polls=st.integers(min_value=0, max_value=400),
    never_stops=st.booleans(),
    refuses_stop=st.booleans(),
)
def test_admission_opens_only_after_a_verified_stop(
    tmp_path_factory: pytest.TempPathFactory,
    polls: int,
    *,
    never_stops: bool,
    refuses_stop: bool,
) -> None:
    """Whatever the cache does, the cache is never started before it is proven stopped.

    Either start-up succeeds having stopped the cache first with the barrier in
    place, or it fails and the cache is not started and the barrier remains.
    """
    tmp_path = tmp_path_factory.mktemp("quiesce")
    unit = CacheUnit(
        barrier=tmp_path / "run" / "cache-recovery",
        shows_to_stop=polls,
        never_stops=never_stops,
        refuses_stop=refuses_stop,
    )
    clock = FakeClock()
    cache = ManagedSccache("c.service", tmp_path / "c.sock", UserManager(unit.runner))
    barrier = RecoveryBarrier(unit.barrier)
    try:
        stopped = quiesce_live_cache(cache, barrier, clock.timing())
    except StartupAccountingError:
        assert "start" not in unit.jobs, "an unaccounted cache is never started"
        assert unit.barrier.exists(), "the barrier outlives a failed quiesce"
        return
    assert stopped, "a live cache that quiesced was stopped"
    assert unit.barrier_at_job["stop"], "the barrier preceded the stop"
    assert unit.state == STOPPED, "success means the strict check passed"


@pytest.mark.parametrize(
    ("refuses_stop", "never_stops", "outcome"),
    [
        (False, False, "stopped"),
        (True, False, "refused"),
        (False, True, "timed_out"),
    ],
    ids=["stopped", "refused", "timed-out"],
)
def test_the_start_up_stop_reports_how_it_ended_once(
    tmp_path: Path, outcome: str, *, refuses_stop: bool, never_stops: bool
) -> None:
    """A stop of a live cache is metered with its outcome and bounded duration."""
    unit = CacheUnit(
        barrier=tmp_path / "run" / "cache-recovery",
        refuses_stop=refuses_stop,
        never_stops=never_stops,
    )
    clock = FakeClock()
    cache = ManagedSccache("c.service", tmp_path / "c.sock", UserManager(unit.runner))
    reports: list[tuple[str, float]] = []
    outcomes = {
        "stopped": nullcontext(),
        "refused": pytest.raises(StartupAccountingError),
        "timed_out": pytest.raises(QuiesceTimeoutError),
    }
    with outcomes[outcome]:
        quiesce_and_report(
            cache,
            RecoveryBarrier(unit.barrier),
            clock.timing(),
            lambda name, seconds: reports.append((name, seconds)),
        )
    assert [name for name, _seconds in reports] == [outcome], "reported exactly once"
    assert 0.0 <= reports[0][1] <= 46.0, "the duration is bounded by the timeout"


def test_a_cache_that_was_not_live_reports_nothing(tmp_path: Path) -> None:
    """Only a start-up that acted on a live cache is metered."""
    unit = CacheUnit(barrier=tmp_path / "run" / "cache-recovery", state=STOPPED)
    cache = ManagedSccache("c.service", tmp_path / "c.sock", UserManager(unit.runner))
    reports: list[tuple[str, float]] = []
    quiesce_and_report(
        cache,
        RecoveryBarrier(unit.barrier),
        FakeClock().timing(),
        lambda name, seconds: reports.append((name, seconds)),
    )
    assert not reports, "a stopped cache is neither a success nor a failure"


def test_the_watch_emits_the_quiesce_event_with_bounded_fields(
    tmp_path: Path, metric_sink: RecordingSink
) -> None:
    """The supervisor's own metrics port receives the validated record."""
    unit = CacheUnit(barrier=tmp_path / "run" / "cache-recovery")
    manager = UserManager(unit.runner)
    server = ManagedSccache("c.service", tmp_path / "c.sock", manager)
    watch = ServerWatch(server, Admission(), "sccache", metrics=Metrics(metric_sink))
    watch.quiesce_live_cache(RecoveryBarrier(unit.barrier), FakeClock().timing())
    assert metric_sink.records[0]["BUILD_LIMITS_EVENT"] == "cache_quiesce", (
        "the event is the cache_quiesce record"
    )
    assert metric_sink.records[0]["BUILD_LIMITS_QUIESCE_OUTCOME"] == "stopped", (
        "the outcome is a token from the closed vocabulary"
    )
