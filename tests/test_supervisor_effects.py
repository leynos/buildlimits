"""Direct unit coverage of the supervisor's effect handlers.

These tests drive the handlers :meth:`Supervisor._return_token`,
:meth:`Supervisor._shutdown`, :meth:`Supervisor.stop` and
:meth:`Supervisor._launch` directly, replacing the token pool and the
operating-system side of compiler groups with small fakes so that token
accounting, exit-status semantics and launch outcomes are exercised
deterministically.
"""

import dataclasses as dc
import socket
import subprocess
import typing as typ

import pytest
from pool_helpers import FakePool, attach_pool
from request_helpers import crate_pending

from buildlimits.admission import Group, GroupState
from buildlimits.config import SupervisorConfig
from buildlimits.replies import Admitted
from buildlimits.supervisor import Supervisor
from buildlimits.systemd import UserManager

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.groups import Groups
    from buildlimits.pool import TokenPool
    from buildlimits.requesters import Pending


@dc.dataclass(slots=True)
class FakeGroups:
    """A groups double that scripts whether placement succeeds."""

    order: list[str]
    running: object | None
    notice: str = ""

    def place(self, entry: Pending, group_id: str, *, cached: bool) -> object | None:
        """Report the scripted placement outcome, admitting ``entry`` on success."""
        del cached
        if self.running is not None:
            entry.group_id = group_id
        return self.running

    def cache_notice(self, entry: Pending, *, cached: bool) -> str:
        """Report the scripted cache notice."""
        del entry, cached
        return self.notice

    def release(self, running: object) -> None:
        """Record that the group was released."""
        del running
        self.order.append("release")


def _empty_runner(argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Answer every systemd query with no properties and no load."""
    return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def supervisor() -> cabc.Iterator[Supervisor]:
    """Build a supervisor over a quiet manager, closing its selector after."""
    manager = UserManager(_empty_runner)
    instance = Supervisor(SupervisorConfig(), manager, clock=lambda: 0.0)
    yield instance
    instance.selector.close()


# -- _return_token ----------------------------------------------------------------


def test_return_token_failure_makes_the_supervisor_unhealthy(
    supervisor: Supervisor,
) -> None:
    """A write failure returning a token is treated as a loss of health."""
    pool = FakePool(wake_fd=-1, give_back_error=OSError("device gone"))
    supervisor.pool = typ.cast("TokenPool", pool)

    supervisor._return_token(b"a")

    assert supervisor.admission.healthy is False, (
        "a failed return left the pool untrustworthy"
    )


def test_return_token_success_writes_back_the_token(supervisor: Supervisor) -> None:
    """A successful return reaches the pool unchanged."""
    pool = attach_pool(supervisor)

    supervisor._return_token(b"z")

    assert pool.returned == [b"z"], "the token was written back"
    assert supervisor.admission.healthy is True, "a clean return stays healthy"


# -- shutdown exit status -----------------------------------------------------------


def test_shutdown_after_stopping_exits_cleanly(supervisor: Supervisor) -> None:
    """A drain requested by stop() exits with status zero."""
    supervisor._stopping = True

    supervisor._shutdown()

    assert supervisor.exit_status == 0, "an orderly stop exits cleanly"


def test_shutdown_after_a_health_failure_exits_non_zero(
    supervisor: Supervisor,
) -> None:
    """A drain forced by a health failure exits with status one."""
    assert supervisor._stopping is False, "the supervisor was never asked to stop"

    supervisor._shutdown()

    assert supervisor.exit_status == 1, "systemd should restart the supervisor"


def test_stop_drains_and_shuts_down_when_nothing_is_running(
    supervisor: Supervisor,
) -> None:
    """Stopping an idle supervisor drains immediately and exits cleanly."""
    supervisor.stop()

    assert supervisor.admission.healthy is False, "stopping refuses new admissions"
    assert supervisor.exit_status == 0, "nothing was running, so the drain finished"


# -- _launch --------------------------------------------------------------------------


def test_launch_failure_refuses_the_request_and_returns_its_token(
    supervisor: Supervisor,
) -> None:
    """A group that never placed is refused and its token returned."""
    pool = attach_pool(supervisor)
    supervisor.groups = typ.cast("Groups", FakeGroups(order=[], running=None))
    supervisor.admission.groups["g1"] = Group(
        "g1", request_id=1, token=b"a", cached=False, state=GroupState.RUNNING
    )
    supervisor.requests.pending[1] = crate_pending(1)

    supervisor._launch(1, "g1", cached=False)

    assert pool.returned == [b"a"], "the held token returned to the pool"
    assert 1 not in supervisor.requests.pending, "the refused request was forgotten"
    assert "g1" not in supervisor.admission.groups, "the failed group was dropped"


@pytest.mark.parametrize(
    "case",
    [
        ("none", "launch-failed", True),
        ("closing", "restarting", False),
        ("requested", "restarting", False),
        ("immediate", "launch-failed", True),
    ],
)
def test_a_failed_placement_is_retried_only_during_a_graceful_stop(
    supervisor: Supervisor,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, str, bool],
) -> None:
    """A stop that broke the placement must not cost the request its build.

    The ``requested`` case is the incident's: the signal handler has only
    recorded the stop when the failed placement is seen, so admission is not yet
    closing.
    """
    stop, reason, keeps_cause = case
    pool = attach_pool(supervisor)
    supervisor.groups = typ.cast("Groups", FakeGroups(order=[], running=None))
    supervisor.admission.groups["g1"] = Group(
        "g1", request_id=1, token=b"a", cached=False, state=GroupState.RUNNING
    )
    entry = crate_pending(1)
    entry.exec_error = "cannot create x.scope: "
    supervisor.requests.pending[1] = entry
    refusals: list[tuple[int, str, str | None]] = []
    monkeypatch.setattr(
        supervisor.requests,
        "refuse",
        lambda request_id, why: refusals.append((request_id, why, entry.exec_error)),
    )
    match stop:
        case "closing":
            supervisor.admission.begin_closing()
        case "requested":
            supervisor.request_graceful_stop()
        case "immediate":
            supervisor.request_stop()

    supervisor._launch(1, "g1", cached=False)

    cause = "cannot create x.scope: " if keeps_cause else None
    assert refusals == [(1, reason, cause)], "the refusal says whether to retry"
    assert pool.returned == [b"a"], "the held token returned exactly once"
    assert "g1" not in supervisor.admission.groups, "the failed group was dropped"
    assert supervisor.admission.closing == (stop in {"closing", "requested"}), (
        "a requested graceful stop is honoured by the time the refusal is made"
    )


def test_launch_success_notifies_before_releasing_the_group(
    supervisor: Supervisor,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A placed group is announced to its requester before it may run."""
    order: list[str] = []
    running = object()
    supervisor.groups = typ.cast("Groups", FakeGroups(order=order, running=running))
    supervisor.requests.pending[1] = crate_pending(1)
    notified: list[tuple[int, object]] = []

    def _notify(request_id: int, reply: object) -> None:
        """Record a notification and its position relative to release."""
        order.append("notify")
        notified.append((request_id, reply))

    monkeypatch.setattr(supervisor.requests, "notify", _notify)

    client, connection = socket.socketpair()
    with client, connection:
        supervisor.requests.pending[1].conn = connection
        supervisor._launch(1, "g1", cached=False)

    assert order == ["notify", "release"], (
        "the requester learns of admission before the group may write"
    )
    assert notified == [(1, Admitted("g1", False, ""))], (
        "the requester was told which group admitted it"
    )
    assert "admitted request 1 as g1" in capsys.readouterr().err, (
        "the admission was logged"
    )


def test_metrics_admission_and_failure_boundaries(
    supervisor, metrics, metric_sink
) -> None:
    """Only placed groups count as admitted; failed placement is terminal once."""
    supervisor.metrics = metrics
    supervisor.requests.metrics = metrics
    supervisor.requests.pending[1] = crate_pending(1)
    supervisor.groups = typ.cast("Groups", FakeGroups(order=[], running=None))
    supervisor.admission.groups["g1"] = Group("g1", 1, b"a", cached=False)
    supervisor._launch(1, "g1", cached=False)
    supervisor.requests.refuse(1, "launch-failed")
    records = metric_sink.records
    assert [r["BUILD_LIMITS_EVENT"] for r in records] == ["request_finished"], (
        "one terminal record"
    )
    assert records[0]["BUILD_LIMITS_REASON"] == "launch_failed", "bounded reason"
    assert records[0]["BUILD_LIMITS_HELD"] == "0", "post-transition gauge"


def test_health_transition_and_stop_metrics_are_once(
    supervisor, metrics, metric_sink
) -> None:
    """Repeated health failures cannot inflate health or shutdown counts."""
    supervisor.metrics = metrics
    supervisor._unhealthy("/secret/token-device")
    supervisor._unhealthy("another secret")
    supervisor._shutdown()
    assert [r["BUILD_LIMITS_EVENT"] for r in metric_sink.records] == [
        "health_changed",
        "supervisor_stopped",
    ], "one transition and one terminal supervisor event"
    assert "secret" not in str(metric_sink.records), (
        "raw pool errors remain outside metrics"
    )


def test_return_token_failure_has_operation_and_gauges(
    supervisor, metrics, metric_sink
) -> None:
    """A pool write failure is distinguished from a pool read failure."""
    supervisor.metrics = metrics
    supervisor.pool = typ.cast(
        "TokenPool", FakePool(-1, give_back_error=OSError("secret"))
    )
    supervisor._return_token(b"a")
    record = metric_sink.records[0]
    assert record["BUILD_LIMITS_EVENT"] == "pool_failure", "write failure counted"
    assert record["BUILD_LIMITS_OPERATION"] == "return", "bounded operation"
    assert record["BUILD_LIMITS_CAPACITY"] == str(supervisor.settings.slots), (
        "snapshot present"
    )


@pytest.mark.parametrize(
    "case",
    [
        (True, True, "cached"),
        (True, False, "direct_fallback"),
        (False, False, "direct"),
    ],
)
def test_successful_admission_reports_cache_path(
    supervisor,
    metrics,
    metric_sink,
    case: tuple[bool, bool, str],
) -> None:
    """Direct fallback counts only cacheable requests configured for sccache."""
    cacheable, cached, path = case
    supervisor.metrics = metrics
    supervisor.requests.pending[1] = crate_pending(1)
    supervisor.requests.pending[1].request = dc.replace(
        supervisor.requests.pending[1].request,
        cacheable=cacheable,
    )
    supervisor.groups = typ.cast("Groups", FakeGroups(order=[], running=object()))
    client, connection = socket.socketpair()
    with client, connection:
        supervisor.requests.pending[1].conn = connection
        supervisor._launch(1, "g1", cached=cached)
    assert metric_sink.records[0]["BUILD_LIMITS_CACHE_PATH"] == path, (
        "cache path bounded"
    )
