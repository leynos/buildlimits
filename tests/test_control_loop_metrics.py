"""Control recovery metrics preserve admission's sole authority over held work."""

import errno
import typing as typ

from pool_helpers import FakePool, attach_pool

from buildlimits.admission import Group, GroupState
from buildlimits.config import SupervisorConfig
from buildlimits.drain_loop import LoopEffects
from buildlimits.supervisor import Supervisor, SupervisorTelemetry
from buildlimits.timing import Clock

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import selectors

    from conftest import RecordingSink

    from buildlimits.metrics import Metrics


class ScriptedSelector:
    """A selector that raises scripted faults before producing complete cycles."""

    def __init__(self, errors: list[Exception | None]) -> None:
        """Keep an explicit finite fault script and count every selection."""
        self.errors = errors
        self.calls = 0

    def select(self, _seconds: float) -> list[object]:
        """Fail once or return an empty ready set for a real tick cycle."""
        self.calls += 1
        error = self.errors.pop(0)
        if error is not None:
            raise error
        return []


def _hold_server_drain(instance: Supervisor) -> FakePool:
    """Attach a scripted pool and leave one cached group awaiting server work.

    Returns
    -------
    FakePool
        The attached pool, whose returns show when the token is accounted.
    """
    pool = attach_pool(instance)
    instance.admission.groups["g1"] = Group(
        "g1", 1, b"a", True, state=GroupState.SERVER_DRAIN
    )
    return pool


def _run_loop(
    instance: Supervisor, selector: ScriptedSelector, tick: cabc.Callable[[], None]
) -> int:
    """Run the supervisor's control loop over a scripted selector.

    Returns
    -------
    int
        The exit status the loop's exit authority approved.
    """
    effects = LoopEffects(
        lambda _event: None, tick, instance._unhealthy, lambda: instance.exit_status
    )
    with instance.selector:
        return instance.loop.run(
            typ.cast("selectors.BaseSelector", selector), effects, tick_seconds=0.1
        )


def test_control_fault_fallback_and_real_recovery_keep_tokens_accounted(
    metrics: Metrics, metric_sink: RecordingSink
) -> None:
    """A successful fallback cannot claim complete control recovery or exit early."""
    sleeps: list[float] = []
    instance = Supervisor(
        SupervisorConfig(),
        clock=Clock(now=lambda: 0.0, sleep=sleeps.append),
        telemetry=SupervisorTelemetry(metrics=metrics),
    )
    pool = _hold_server_drain(instance)
    selector = ScriptedSelector([OSError(errno.EMFILE, "secret selector"), None, None])
    ticks = 0

    def tick() -> None:
        """Keep cached work held until the third independently accounted tick."""
        nonlocal ticks
        ticks += 1
        if ticks < 3:
            assert instance.admission.tokens_held() == 1, "fault recovery retains work"
            assert not pool.returned, "a successful control call cannot return a token"
        else:
            instance._apply(instance.admission.server_activity(0))

    result = _run_loop(instance, selector, tick)
    assert result == 1, "an accounted unhealthy shutdown keeps its failure status"
    assert pool.returned == [b"a"], "only final server accounting returns the token"
    progress = metric_sink.of_event("drain_progress")
    assert [record["BUILD_LIMITS_DRAIN_STATE"] for record in progress] == [
        "failed",
        "pending",
        "recovered",
    ], "fallback progress cannot clear the fault before a full successful cycle"
    assert progress[-1]["BUILD_LIMITS_DRAIN_RECOVERIES"] == "1", (
        "subsequent healthy cycles do not manufacture more recoveries"
    )
    assert sleeps == [0.1], "observation preserves the original control retry delay"
    assert "secret" not in str(progress), "fault content never enters bounded fields"


def test_healthy_control_cycles_emit_no_progress(
    metrics: Metrics, metric_sink: RecordingSink
) -> None:
    """Normal selector and tick cycles do not produce ten-per-second records."""
    instance = Supervisor(
        SupervisorConfig(), telemetry=SupervisorTelemetry(metrics=metrics)
    )
    selector = ScriptedSelector([None, None, None])
    ticks = 0

    def tick() -> None:
        """Permit exit after three fully successful cycles without a fault."""
        nonlocal ticks
        ticks += 1
        if ticks == 3:
            instance.exit_status = 0

    assert _run_loop(instance, selector, tick) == 0, (
        "only the exit authority completes the loop"
    )
    assert not metric_sink.records, (
        "healthy cycles produce no progress or error records"
    )


class BrokenMetrics:
    """An injected downstream emitter that always raises."""

    def emit(self, event: str, **_fields: object) -> None:
        """Fail observation without changing the caller's control operations."""
        del event
        msg = "unavailable metrics"
        raise RuntimeError(msg)


def test_control_metric_failure_preserves_held_token_recovery() -> None:
    """Broken metric ports cannot authorize or prevent accounted token return."""
    instance = Supervisor(
        SupervisorConfig(),
        clock=Clock(now=lambda: 0.0, sleep=lambda _seconds: None),
        telemetry=SupervisorTelemetry(metrics=BrokenMetrics()),
    )
    pool = _hold_server_drain(instance)
    selector = ScriptedSelector([OSError(errno.EIO, "fault"), None])
    ticks = 0

    def tick() -> None:
        """Retain work on the fallback and account for it on a later cycle."""
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            assert instance.admission.tokens_held() == 1, "fallback keeps the token"
        else:
            instance._apply(instance.admission.server_activity(0))

    result = _run_loop(instance, selector, tick)
    assert result == 1, "metric failures preserve admission-approved exit status"
    assert pool.returned == [b"a"], (
        "the held token returns exactly once after accounting"
    )
    assert instance.loop.failures == 1, "metric failures are not control faults"
