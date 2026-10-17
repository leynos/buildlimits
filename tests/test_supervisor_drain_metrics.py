"""Control faults retain tokens until admission accounting permits shutdown."""

import typing as typ

import pytest
from pool_helpers import attach_pool

from buildlimits import supervisor as supervisor_module
from buildlimits.admission import Group, GroupState
from buildlimits.config import SupervisorConfig
from buildlimits.drain_loop import DrainLoop, LoopEffects
from buildlimits.effects import Exit
from buildlimits.supervisor import Supervisor
from buildlimits.timing import Clock

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import selectors

    from conftest import RecordingSink

    from buildlimits.metrics import Metrics


@pytest.fixture
def supervisor(metrics: Metrics) -> cabc.Iterator[Supervisor]:
    """Provide a real supervisor with recording metrics and no retry delay."""
    instance = Supervisor(
        SupervisorConfig(),
        clock=Clock(now=lambda: 0.0, sleep=lambda _seconds: None),
        metrics=metrics,
    )
    yield instance
    instance.selector.close()


# -- serve() ------------------------------------------------------------------------


def test_retry_exhaustion_preserves_pool_ownership_until_scope_accounting(
    monkeypatch: pytest.MonkeyPatch,
    supervisor: Supervisor,
    metric_sink: RecordingSink,
) -> None:
    """A retry limit requires recovery rather than abandoning admitted work."""
    monkeypatch.setattr(supervisor_module, "TICK_SECONDS", 0.0)
    supervisor.admission.groups["g1"] = Group(
        "g1", request_id=1, token=b"a", cached=False, state=GroupState.RUNNING
    )
    pool = attach_pool(supervisor)
    ticks = 0

    def recover() -> None:
        """Confirm scope completion only after the former fatal retry bound."""
        nonlocal ticks
        ticks += 1
        if ticks <= supervisor_module.MAX_INTERNAL_FAILURES + 2:
            assert supervisor.admission.tokens_held() == 1, "retrying keeps the token"
            assert not pool.returned, "fault count is not scope completion"
            msg = "injected control fault"
            raise RuntimeError(msg)
        supervisor._apply(supervisor.admission.leader_exited("g1", Exit(code=0)))
        supervisor._apply(supervisor.admission.group_emptied("g1"))

    monkeypatch.setattr(supervisor, "_tick", recover)
    assert supervisor.serve() == 1, "verified unhealthy completion exits nonzero"
    assert supervisor._failures == supervisor_module.MAX_INTERNAL_FAILURES + 2, (
        "each injected failure is counted without a fatal retry shortcut"
    )
    assert pool.returned == [b"a"], "confirmed completion returns the token once"
    assert not supervisor.admission.groups, "exit follows complete admission accounting"
    errors = metric_sink.of_event("internal_error")
    assert len(errors) == supervisor._failures, "one metric per counted fault"
    assert errors[-1]["BUILD_LIMITS_FAILURES"] == str(supervisor._failures), (
        "the final fault metric carries the current bounded count"
    )
    stopped = metric_sink.of_event("supervisor_stopped")
    assert len(stopped) == 1, "accounted shutdown emits once"
    assert stopped[0]["BUILD_LIMITS_OUTCOME"] == "failed", (
        "recovered health failure retains a non-zero exit classification"
    )


def test_repeated_loop_faults_hold_cached_work_until_accounted_recovery(
    monkeypatch: pytest.MonkeyPatch, supervisor: Supervisor
) -> None:
    """Retry exhaustion retains cached work until the accounting port confirms completion."""
    monkeypatch.setattr(supervisor_module, "TICK_SECONDS", 0.0)
    supervisor.admission.groups["g1"] = Group(
        "g1", request_id=1, token=b"a", cached=True, state=GroupState.SERVER_DRAIN
    )
    pool = attach_pool(supervisor)
    ticks = 0

    def recover() -> None:
        """Confirm stopped empty service accounting after the former fatal retry bound."""
        nonlocal ticks
        ticks += 1
        if ticks <= supervisor_module.MAX_INTERNAL_FAILURES + 2:
            raise OSError(24, "injected pressure/query transport failure")
        if ticks == supervisor_module.MAX_INTERNAL_FAILURES + 3:
            supervisor._apply(supervisor.admission.server_activity(1))
            assert not pool.returned, "live server work still retains the token"
        else:
            supervisor._apply(supervisor.admission.server_activity(0))

    monkeypatch.setattr(supervisor, "_tick", recover)
    assert supervisor.serve() == 1, "unhealthy completion keeps a failing exit status"
    assert ticks == supervisor_module.MAX_INTERNAL_FAILURES + 4, (
        "retry exhaustion does not bypass drain recovery"
    )
    assert pool.returned == [b"a"], (
        "verified stopped work returns its token exactly once"
    )
    assert supervisor.admission.tokens_held() == 0, (
        "shutdown follows complete accounting"
    )


@pytest.mark.parametrize("broken_metrics", [False, True])
def test_drain_loop_uses_injected_sleep_until_accounting_permits_exit(
    broken_metrics: bool,
) -> None:
    """An unavailable selector waits through its port without releasing work early."""
    sleeps: list[float] = []
    failures: list[str] = []
    observations: list[int] = []
    ticks = 0

    class FaultySelector:
        """Keep the selector unavailable until the independent drain poll recovers."""

        def select(self, _timeout: float) -> list[object]:
            """Fail before returning any requester or token-pool events."""
            raise OSError(24, "injected selector pressure")

    def tick() -> None:
        """Account for existing work only after two separately paced attempts."""
        nonlocal ticks
        ticks += 1
        assert len(sleeps) == ticks, "drain polling follows the injected wait"

    def observe(count: int) -> None:
        """Record faults even when the injected metric port subsequently fails."""
        observations.append(count)
        if broken_metrics:
            msg = "injected metric failure"
            raise RuntimeError(msg)

    loop = DrainLoop(sleep=sleeps.append, failure_observer=observe)
    effects = LoopEffects(
        dispatch=lambda _event: None,
        tick=tick,
        fail_health=failures.append,
        exit_status=lambda: 1 if ticks == 2 else None,
    )
    assert (
        loop.run(
            typ.cast("selectors.BaseSelector", FaultySelector()),
            effects,
            tick_seconds=0.25,
        )
        == 1
    ), "the loop exits only after the accounting port permits it"
    assert sleeps == [0.25, 0.25], "the dependency receives the configured retry delay"
    assert len(failures) == loop.failures == 2, (
        "each failure preserves unhealthy accounting"
    )

    assert observations == [1, 2], "metric failures cannot change drain accounting"
