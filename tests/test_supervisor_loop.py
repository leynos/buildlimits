"""Direct unit coverage of the supervisor's event loop and failure handling.

These tests drive :class:`buildlimits.supervisor.Supervisor` without a real
token device, systemd manager or compiler, replacing its collaborators with
small fakes so that dispatch, pool events, health checks, the ``serve`` loop
and effect handling can be exercised deterministically.
"""

import collections.abc as cabc
import os
import subprocess
import typing as typ
from pathlib import Path, PurePosixPath

import pytest
from pool_helpers import FakePool

from buildlimits import startup
from buildlimits import supervisor as supervisor_module
from buildlimits.admission import Admission
from buildlimits.config import SupervisorConfig
from buildlimits.pool import PoolFailure, Token
from buildlimits.supervisor import Supervisor, SupervisorTelemetry
from buildlimits.supervisor_polling import HEALTH_POLL_SECONDS
from buildlimits.systemd import UserManager
from buildlimits.timing import Clock

if typ.TYPE_CHECKING:
    from conftest import RecordingSink

    from buildlimits.metrics import Metrics
    from buildlimits.pool import TokenPool

OWN_CGROUP = PurePosixPath(
    "/user.slice/user-1000.slice/user@1000.service/app.slice/s.service"
)


@pytest.fixture
def supervisor() -> cabc.Iterator[Supervisor]:
    """Build a supervisor over a quiet manager, closing its selector after."""
    manager = UserManager(_empty_runner)
    instance = Supervisor(
        SupervisorConfig(),
        manager,
        clock=Clock(now=lambda: 0.0, sleep=lambda _seconds: None),
    )
    yield instance
    instance.selector.close()


def _empty_runner(argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Answer manager queries with a verified stopped, empty cache service."""
    return subprocess.CompletedProcess(
        argv, 0, "MainPID=0\nControlGroup=\nActiveState=inactive\n", ""
    )


# -- start() ------------------------------------------------------------------


def test_start_opens_the_pool_and_registers_the_listener(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    metrics,
    metric_sink,
) -> None:
    """Start-up opens a token pool, registers the listener, and refreshes caching."""
    monkeypatch.setattr(startup, "drain_slice", lambda *_args: 0)
    monkeypatch.setattr(Supervisor, "_watch_signals", lambda self: None)
    opened: list[Path] = []
    pipe_fds: list[int] = []

    def _fake_pool(path: Path) -> FakePool:
        """Record which device path was opened and hand back a fake pool."""
        opened.append(path)
        read_end, write_end = os.pipe2(os.O_CLOEXEC)
        pipe_fds.extend((read_end, write_end))
        return FakePool(wake_fd=read_end)

    monkeypatch.setattr(supervisor_module, "TokenPool", _fake_pool)
    settings = SupervisorConfig(
        socket_path=tmp_path / "run" / "supervisor.sock",
        sccache_socket=tmp_path / "run" / "sccache.sock",
        cache_mode="sccache",
    )
    instance = Supervisor(
        settings,
        UserManager(_empty_runner),
        clock=lambda: 0.0,
        telemetry=SupervisorTelemetry(metrics=metrics),
    )
    try:
        instance.start(OWN_CGROUP)
        assert metric_sink.records[-1]["BUILD_LIMITS_EVENT"] == "supervisor_started", (
            "start after barrier"
        )
        assert opened == [settings.device], "the configured device was opened"
        assert isinstance(instance.pool, FakePool), "the fake pool was installed"
        assert instance.listener is not None, "the listener was created"
        registered = {key.fd for key in instance.selector.get_map().values()}
        assert instance.pool.wake_fd in registered, "the pool's wake descriptor waits"
        assert instance.listener.fileno() in registered, "the listener waits too"
        # An unreachable socket path means the server is not reachable; the
        # refresh still runs and records that outcome rather than erroring.
        assert instance.admission.cached is False, "cache mode was refreshed"
    finally:
        if instance.listener is not None:
            instance.listener.close()
        instance.selector.close()
        for fd in pipe_fds:
            os.close(fd)


# -- _dispatch ------------------------------------------------------------------


def test_dispatch_routes_each_tag_to_its_handler(
    supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every selector tag reaches the handler that owns it."""
    calls: list[str] = []

    def _accept() -> None:
        """Record that the listener tag dispatched here."""
        calls.append("listener")

    def _pool_events() -> None:
        """Record that the pool tag dispatched here."""
        calls.append("pool")

    def _connection_ready(request_id: int) -> None:
        """Record which connection tag dispatched here."""
        calls.append(f"conn:{request_id}")

    def _exec_outcome(group_id: str) -> None:
        """Record which errors tag dispatched here."""
        calls.append(f"errors:{group_id}")

    def _reap_leaders() -> None:
        """Record that the signals tag dispatched here."""
        calls.append("signals")

    monkeypatch.setattr(supervisor, "_accept", _accept)
    monkeypatch.setattr(supervisor, "_pool_events", _pool_events)
    monkeypatch.setattr(supervisor, "_connection_ready", _connection_ready)
    monkeypatch.setattr(supervisor, "_exec_outcome", _exec_outcome)
    monkeypatch.setattr(supervisor, "_reap_leaders", _reap_leaders)

    supervisor._dispatch("listener")
    supervisor._dispatch("pool")
    supervisor._dispatch(("conn", 7))
    supervisor._dispatch(("errors", "g1"))
    supervisor._dispatch("signals")

    assert calls == [
        "listener",
        "pool",
        "conn:7",
        "errors:g1",
        "signals",
    ], "each tag reached its own handler, in order"


# -- _pool_events ---------------------------------------------------------------


def test_pool_events_feeds_a_token_to_admission(
    monkeypatch: pytest.MonkeyPatch, supervisor: Supervisor
) -> None:
    """A token read from the pool is handed to admission's token_arrived."""
    fed: list[bytes] = []

    def _record(self: Admission, token: bytes) -> list[object]:
        """Record the token handed to admission and report no effects."""
        del self
        fed.append(token)
        return []

    monkeypatch.setattr(Admission, "token_arrived", _record)
    pool = FakePool(wake_fd=-1, scripted=[Token(b"a")])
    supervisor.pool = typ.cast("TokenPool", pool)

    supervisor._pool_events()

    assert fed == [b"a"], "the token's value reached admission"


def test_pool_events_a_failure_makes_the_supervisor_unhealthy(
    supervisor: Supervisor, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pool failure stops admission and starts draining."""
    pool = FakePool(wake_fd=-1, scripted=[PoolFailure("reading a token failed: EIO")])
    supervisor.pool = typ.cast("TokenPool", pool)

    supervisor._pool_events()

    assert supervisor.admission.healthy is False, "the pool failure was fatal"
    assert "reading a token failed: EIO" in capsys.readouterr().err, (
        "the failure's reason was logged"
    )


# -- health poll ------------------------------------------------------------------


def test_tick_detects_a_replaced_device(
    supervisor: Supervisor,
    capsys: pytest.CaptureFixture[str],
    metrics: Metrics,
    metric_sink: RecordingSink,
) -> None:
    """Replacement reports failure before health changes, without later duplicates."""
    supervisor.metrics = metrics
    pool = FakePool(wake_fd=-1, same_device=False)
    supervisor.pool = typ.cast("TokenPool", pool)
    supervisor._next_health_poll = -1.0
    supervisor._next_server_poll = float("inf")
    supervisor._next_cache_poll = float("inf")

    supervisor._tick()

    assert supervisor.admission.healthy is False, "a replaced device is unhealthy"
    assert f"{supervisor.settings.device} was replaced" in capsys.readouterr().err, (
        "the replacement reason was logged"
    )
    events = [record["BUILD_LIMITS_EVENT"] for record in metric_sink.records]
    assert events[:2] == ["pool_failure", "health_changed"], "failure precedes health"
    assert events.count("pool_failure") == events.count("health_changed") == 1, "once"
    assert metric_sink.records[0]["BUILD_LIMITS_OPERATION"] == "device", (
        "failure identifies the device operation"
    )
    assert metric_sink.records[1]["BUILD_LIMITS_HEALTH"] == "unhealthy", (
        "health transition identifies the unhealthy state"
    )
    recorded = tuple(metric_sink.records)
    supervisor.clock = lambda: HEALTH_POLL_SECONDS
    supervisor._tick()
    assert pool.device_checks == 2, "replacement is checked again when next due"
    assert tuple(metric_sink.records) == recorded, "no repeated transition metrics"


def test_tick_leaves_a_stable_device_healthy(
    supervisor: Supervisor, metrics: Metrics, metric_sink: RecordingSink
) -> None:
    """An unchanged device passes the health poll without complaint."""
    supervisor.metrics = metrics
    pool = FakePool(wake_fd=-1, same_device=True)
    supervisor.pool = typ.cast("TokenPool", pool)
    supervisor._next_health_poll = -1.0
    supervisor._next_server_poll = float("inf")
    supervisor._next_cache_poll = float("inf")

    supervisor._tick()

    assert supervisor.admission.healthy is True, "nothing was wrong"
    assert not metric_sink.records, "stable identity emits no failure or transition"


@pytest.mark.parametrize(
    "case",
    [
        pytest.param((9.0, False, True, 0, 10.0), id="before-deadline"),
        pytest.param((10.0, True, True, 1, 12.0), id="at-deadline"),
        pytest.param((10.0, None, True, 0, 12.0), id="no-pool"),
        pytest.param((10.0, True, False, 1, 12.0), id="unhealthy-stable"),
        pytest.param((10.0, False, False, 1, 12.0), id="unhealthy-replaced"),
    ],
)
def test_tick_health_poll_deadlines(
    supervisor: Supervisor,
    metrics: Metrics,
    metric_sink: RecordingSink,
    case: tuple[float, bool | None, bool, int, float],
) -> None:
    """Only due polls check devices, advancing deadlines even without a pool."""
    now, same_device, healthy, checks, deadline = case
    pool = FakePool(wake_fd=-1, same_device=bool(same_device))
    supervisor.pool = typ.cast("TokenPool", pool) if same_device is not None else None
    supervisor.admission.healthy = healthy
    supervisor.metrics = metrics
    times = iter([now])
    supervisor.clock = lambda: next(times)
    supervisor._next_health_poll = 10.0
    supervisor._next_server_poll = supervisor._next_cache_poll = float("inf")
    supervisor._tick()
    assert pool.device_checks == checks, "one check per due poll with a pool"
    assert supervisor._next_health_poll == pytest.approx(deadline), (
        "deadline advances only when due"
    )
    assert supervisor.admission.healthy is healthy, "no new health transition"
    assert all(
        record["BUILD_LIMITS_EVENT"] not in {"pool_failure", "health_changed"}
        for record in metric_sink.records
    ), "no device-failure or health metric"


def test_tick_preserves_periodic_poll_order(
    supervisor: Supervisor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One clock read precedes server, device and cache polling in that order."""
    calls: list[str] = []

    def _clock() -> float:
        """Record the sole monotonic clock read."""
        calls.append("clock")
        return 10.0

    def _server_poll(_self: object) -> list[object]:
        """Record server polling without producing admission effects."""
        calls.append("server")
        return []

    def _device_poll(self: FakePool) -> bool:
        """Check scheduling has advanced before recording stable identity."""
        assert supervisor._next_health_poll == pytest.approx(12.0), (
            "schedule before device check"
        )
        self.device_checks += 1
        calls.append("health")
        return True

    def _cache_poll(_self: object) -> None:
        """Record cache polling."""
        calls.append("cache")

    monkeypatch.setattr(type(supervisor.server), "drained_tokens", _server_poll)
    monkeypatch.setattr(type(supervisor.server), "refresh_cache_mode", _cache_poll)
    monkeypatch.setattr(FakePool, "is_same_device", _device_poll)
    supervisor.pool = typ.cast("TokenPool", FakePool(wake_fd=-1))
    supervisor.clock = _clock
    supervisor._tick()
    assert calls == ["clock", "server", "health", "cache"], "periodic poll order"
