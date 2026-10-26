"""Every loop-facing outcome-shell method is fail-closed and total."""

import dataclasses as dc
import selectors
import typing as typ
from pathlib import Path
from unittest import mock

import pytest
from outcome_shell_test_support import make_outcome_world
from request_helpers import crate_pending

from buildlimits.admission import Admission, Launch
from buildlimits.drain_loop import DrainLoop, LoopEffects
from buildlimits.effects import Effect, Exit, ReturnToken, Shutdown, Terminate
from buildlimits.supervisor_outcome_setup import GroupEnding

if typ.TYPE_CHECKING:
    from buildlimits.pending_request import Pending
    from buildlimits.supervisor_outcomes import OutcomeShell


@dc.dataclass(slots=True)
class _FaultedLoop:
    """Keep one injected shell failure inside the ordinary drain callbacks."""

    shell: OutcomeShell
    admission: Admission
    pending: Pending
    launch: Launch
    returned: list[bytes] = dc.field(default_factory=list)
    stopped: bool = False
    ticks: int = 0
    attempts: int = 0

    def dispatch(self, _tag: object) -> None:
        """Ignore selector events because this deterministic loop has none."""

    def apply(self, effects: list[Effect]) -> None:
        """Observe token ownership and the admission-approved stop effect."""
        for effect in effects:
            if isinstance(effect, ReturnToken):
                self.returned.append(effect.token)
            if isinstance(effect, Shutdown):
                self.stopped = True

    def tick(self) -> None:
        """Inject the shell fault while the real drain loop owns progress."""
        self.attempts += 1
        now = float(self.ticks)
        if self.ticks == 0:
            self.shell.launched(self.pending, self.launch.group_id)
            self.apply(self.admission.begin_closing())
        else:
            effects = self.admission.leader_exited(self.launch.group_id, Exit(code=0))
            assert effects == [Terminate(self.launch.group_id)], (
                "tick: effects == [Terminate(self.launch.group_id)]"
            )
            self.apply(self.admission.group_emptied(self.launch.group_id))
            self.shell.emptied(GroupEnding(self.launch.group_id, True, True))
        self.shell.on_tick(now)
        self.shell.describe()
        self.ticks += 1

    def fail_health(self, _reason: str) -> None:
        """Delegate any loop failure to the actual admission fail-safe."""
        self.apply(self.admission.health_failed())

    def exit_status(self) -> int | None:
        """Stop if a broken shell never reaches graceful shutdown."""
        if self.stopped:
            return 0
        return 97 if self.attempts >= 3 else None


@pytest.mark.parametrize(
    "entry",
    ["launched", "emptied", "on_tick", "describe", "close"],
)
def test_shell_entry_points_cannot_break_graceful_admission_drain(
    tmp_path: Path, entry: str
) -> None:
    """Verify shell faults leave loop health and token authority intact."""
    world = make_outcome_world(tmp_path)
    shell = typ.cast("OutcomeShell", world.shell)
    admission = world.admission
    assert admission.submit(9), "one request enters the admission queue"
    launch = next(
        effect for effect in admission.token_arrived(b"+") if isinstance(effect, Launch)
    )
    pending = crate_pending(9, launch.group_id, admitted=0.0)
    pending.cache_path = "cached"
    callbacks = _FaultedLoop(shell, admission, pending, launch)
    loop = DrainLoop(sleep=lambda _seconds: None)

    with (
        mock.patch.object(
            shell,
            f"_{entry}",
            side_effect=RuntimeError("private diagnostic"),
        ),
        selectors.SelectSelector() as selector,
    ):
        result = loop.run(
            selector,
            LoopEffects(
                dispatch=callbacks.dispatch,
                tick=callbacks.tick,
                fail_health=callbacks.fail_health,
                exit_status=callbacks.exit_status,
            ),
            tick_seconds=0.0,
        )
        shell.close()

    assert result == 0 and callbacks.stopped, (
        "graceful stop completes through admission"
    )
    assert loop.failures == 0, "the shell boundary keeps faults out of the loop"
    assert admission.healthy, "recording faults do not fail admission health"
    assert callbacks.returned == [b"+"], (
        "every held pool token is returned exactly once"
    )
    assert not admission.groups, "the group is removed only after its clean drain"
    assert callbacks.ticks == 2, "the loop advances from launch to completed drain"
    assert world.shell.describe() == {
        "enabled": False,
        "reason": "recorder_disabled",
    }, "the failed shell exposes only its bounded disabled state"
