"""Bounded control-loop fault reporting while existing work keeps its tokens.

Only Supervisor uses these effects. The loop has no authority to close a token
pool or infer compiler idleness; its exit port becomes non-null only when
admission has accounted for existing work. Retry exhaustion requires recovery,
never an early process exit that would reclaim an unaccounted pool balance.
"""

import contextlib
import dataclasses as dc
import typing as typ

from buildlimits.drain_metrics import DrainMetrics
from buildlimits.journal import log
from buildlimits.timing import SYSTEM_CLOCK

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import selectors

MAX_INTERNAL_FAILURES = 20


@dc.dataclass(frozen=True, slots=True)
class LoopEffects:
    """Supervisor effects observed by the draining control loop.

    Attributes
    ----------
    dispatch : Callable[[object], None]
        Handle a selector event.
    tick : Callable[[], None]
        Poll cgroup and server drains, health and pressure recovery.
    fail_health : Callable[[str], None]
        Stop admission after an infrastructure failure.
    exit_status : Callable[[], int | None]
        Admission-approved exit, or None while work remains unaccounted.
    """

    dispatch: cabc.Callable[[object], None]
    tick: cabc.Callable[[], None]
    fail_health: cabc.Callable[[str], None]
    exit_status: cabc.Callable[[], int | None]


class DrainLoop:
    """Keep the pool owned while recovering from control-loop failures.

    Parameters
    ----------
    sleep : Callable[[float], None]
        Wait between control-loop failures without changing exit authority.
    failure_observer : Callable[[int], None] | None
        Observe each counted fault through a failure-isolated metrics port.
    drain_metrics : DrainMetrics | None
        Shared operation observer with no authority to release tokens.
    """

    def __init__(
        self,
        *,
        sleep: cabc.Callable[[float], None] = SYSTEM_CLOCK.sleep,
        failure_observer: cabc.Callable[[int], None] | None = None,
        drain_metrics: DrainMetrics | None = None,
    ) -> None:
        """Start without failures or an operator recovery requirement."""
        self.sleep = sleep
        self.failure_observer = failure_observer
        self.drain_metrics = (
            drain_metrics if drain_metrics is not None else DrainMetrics()
        )
        self.failures = 0
        self.recovery_action: str | None = None

    def run(
        self,
        selector: selectors.BaseSelector,
        effects: LoopEffects,
        *,
        tick_seconds: float,
    ) -> int:
        """Poll until admission permits exit, including after repeated faults.

        Parameters
        ----------
        selector : selectors.BaseSelector
            Existing control watches; registered requester sockets stay live.
        effects : LoopEffects
            Supervisor operations and its sole exit authority.
        tick_seconds : float
            Poll interval, also preventing a persistent selector fault from spinning.

        Returns
        -------
        int
            Admission's approved exit status after compiler accounting completes.
        """
        while (status := effects.exit_status()) is None:
            try:
                self.drain_metrics.control_cycle(
                    lambda: self._cycle(selector, effects, tick_seconds)
                )
            except Exception as error:  # ruff: ignore[blind-except] - retain the pool until admission permits exit.
                self._failure(error, effects.fail_health)
                self.sleep(tick_seconds)
                # An unavailable selector must not starve drain polling.
                try:
                    self.drain_metrics.call(
                        "control_loop", effects.tick, pending=lambda _result: True
                    )
                except Exception as retry_error:  # ruff: ignore[blind-except] - recovery failure cannot authorize exit.
                    self._failure(retry_error, effects.fail_health)
        return status

    @staticmethod
    def _cycle(
        selector: selectors.BaseSelector, effects: LoopEffects, tick_seconds: float
    ) -> None:
        """Complete one selector, dispatch and tick cycle before claiming recovery."""
        for key, _mask in selector.select(tick_seconds):
            effects.dispatch(key.data)
        effects.tick()

    def _failure(
        self, error: Exception, fail_health: cabc.Callable[[str], None]
    ) -> None:
        """Deduplicate faults and keep unknown work covered during recovery."""
        self.failures += 1
        if self.failure_observer is not None:
            with contextlib.suppress(Exception):
                self.failure_observer(self.failures)
        if self.failures == 1:
            with contextlib.suppress(OSError):
                log(
                    f"internal error; draining before exit: {type(error).__name__} "
                    f"errno={getattr(error, 'errno', None)}"
                )
        if self.failures > MAX_INTERNAL_FAILURES and self.recovery_action is None:
            self.recovery_action = (
                "Restore the failed supervisor control path; tokens stay held "
                "until existing compiler work drains. Do not restart active groups."
            )
            with contextlib.suppress(OSError):
                log(f"drain requires operator action: {self.recovery_action}")
        try:
            fail_health("internal error; draining before exit")
        except Exception:  # ruff: ignore[blind-except] - failed termination effects cannot release tokens.
            self.recovery_action = (
                "Restore supervisor drain controls; "
                "existing compiler tokens remain held."
            )
