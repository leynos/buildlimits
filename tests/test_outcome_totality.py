"""Every loop-facing outcome-shell method is fail-closed and total."""

import dataclasses as dc
import selectors
import time
import typing as typ
from pathlib import Path
from unittest import mock

import pytest
from outcome_shell_test_support import (
    FakeWriter,
    OutcomeWorldOptions,
    make_outcome_world,
)
from request_helpers import crate_pending

from buildlimits.admission import Admission, Launch
from buildlimits.drain_loop import DrainLoop, LoopEffects
from buildlimits.effects import Effect, Exit, ReturnToken, Shutdown, Terminate
from buildlimits.supervisor_outcome_setup import GroupEnding

if typ.TYPE_CHECKING:
    from buildlimits.nonblocking_child import ChildReaper
    from buildlimits.outcome_ports import OutcomeStorePort
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


class _StickyReaper:
    """Own one helper that never settles, so a reap loop cannot empty it."""

    def __init__(self, *, fails: bool = False) -> None:
        """Create a reaper with one permanently owned child."""
        self.polls = 0
        self._fails = fails

    @property
    def active_count(self) -> int:
        """Report the one child this fake never releases.

        Returns
        -------
        int
            Always one, so only a deadline can end a reap loop.
        """
        return 1

    def poll(self, now: float) -> None:
        """Count one poll, optionally failing like a broken reaper."""
        del now
        self.polls += 1
        if self._fails:
            raise RuntimeError("reaper diagnostic")


class _StalledWriter(FakeWriter):
    """Spend its whole allowance inside close, like a stalled join."""

    def __init__(self, *, fails: bool = False) -> None:
        """Create a writer that stalls for whatever allowance it is given."""
        super().__init__()
        self._fails = fails

    def close(self, timeout: float) -> None:
        """Record the allowance, stall for it, then optionally fail."""
        self.closed.append(timeout)
        if self._fails:
            raise RuntimeError("writer diagnostic")
        time.sleep(max(0.0, timeout))
        self.is_closed = True


def _faulted_world(
    tmp_path: Path, reaper: _StickyReaper, writer: _StalledWriter
) -> tuple[OutcomeShell, _StickyReaper, _StalledWriter]:
    """Build a world whose owned helper and writer never finish promptly.

    Returns
    -------
    tuple[OutcomeShell, _StickyReaper, _StalledWriter]
        The shell, its sticky reaper and its stalled writer.
    """
    world = make_outcome_world(
        tmp_path,
        OutcomeWorldOptions(
            reaper=typ.cast("ChildReaper", reaper),
            writer=typ.cast("OutcomeStorePort", writer),
        ),
    )
    return typ.cast("OutcomeShell", world.shell), reaper, writer


def test_failure_path_does_not_wait_for_owned_resources(tmp_path: Path) -> None:
    """A recorder fault asks its writer to stop instead of joining it."""
    shell, reaper, writer = _faulted_world(tmp_path, _StickyReaper(), _StalledWriter())

    with mock.patch.object(shell, "_on_tick", side_effect=RuntimeError("diagnostic")):
        shell.on_tick(0.0)

    assert reaper.polls == 1, "the fault path polls its helper exactly once"
    assert writer.closed == [0.0], (
        "the fault path only requests writer shutdown, without waiting"
    )

    shell.on_tick(0.1)
    shell.on_tick(0.2)
    assert reaper.polls == 3, "later ticks keep making one non-blocking step"
    assert writer.closed == [0.0, 0.0, 0.0], "no later tick waits for the store"

    started = time.monotonic()
    shell.close(0.05)
    assert time.monotonic() - started < 0.5, "shutdown stays within its bound"
    assert len(writer.closed) == 4, "shutdown closes the writer once"
    assert shell.describe() == {
        "enabled": False,
        "reason": "recorder_disabled",
    }, "the failed shell still exposes only its bounded disabled state"

    polls_at_close = reaper.polls
    shell.on_tick(0.3)
    assert reaper.polls == polls_at_close, "a closed shell is inert"


def test_failure_cleanup_survives_secondary_exceptions(tmp_path: Path) -> None:
    """Cleanup faults are contained, and one does not skip the other."""
    shell, reaper, writer = _faulted_world(
        tmp_path, _StickyReaper(fails=True), _StalledWriter(fails=True)
    )

    with mock.patch.object(shell, "_launched", side_effect=RuntimeError("diagnostic")):
        shell.launched(crate_pending(1), "g1")

    assert reaper.polls == 1, "the failing reaper was still attempted"
    assert writer.closed == [0.0], "a reaper fault does not prevent the writer request"

    shell.on_tick(0.0)
    assert (reaper.polls, writer.closed) == (2, [0.0, 0.0]), (
        "later ticks keep attempting both cleanup steps"
    )

    shell.close(0.05)
    assert writer.closed[-1] > 0.0, (
        "a failing reaper still leaves the writer its shutdown allowance"
    )
    assert shell.describe() == {
        "enabled": False,
        "reason": "recorder_disabled",
    }, "cleanup faults never escape as a shell failure result"
