"""Outcome polling stays non-blocking under the supervisor's loop cadence."""

import collections.abc as cabc
import os
import subprocess
import sys
import time
import typing as typ
from pathlib import Path

import pytest
from outcome_shell_test_support import OutcomeWorldOptions, make_outcome_world

from buildlimits.admission import Launch
from buildlimits.admission_state import GroupState
from buildlimits.effects import Exit, ReturnToken, Terminate
from buildlimits.nonblocking_child import ChildReaper
from buildlimits.outcome_queries import SccacheStatistics
from buildlimits.outcome_recorder_types import Querying

if typ.TYPE_CHECKING:
    from buildlimits.supervisor_outcomes import OutcomeShell


def test_loop_admits_while_query_stalls(tmp_path: Path) -> None:
    """Ten loop ticks admit work while the real helper remains pending."""
    client = tmp_path / "sccache-stall"
    client.write_text(
        f"#!{sys.executable}\nimport time\ntime.sleep(30)\n", encoding="utf-8"
    )
    client.chmod(0o700)
    reaper = ChildReaper()
    statistics = SccacheStatistics(
        str(client),
        {"PATH": os.environ.get("PATH", "")},
        tmp_path / "sccache.sock",
        reaper=reaper,
    )
    world = make_outcome_world(
        tmp_path,
        OutcomeWorldOptions(statistics_port=statistics, reaper=reaper),
    )
    shell = typ.cast("OutcomeShell", world.shell)
    assert shell._reaper is reaper and statistics._reaper is reaper, (
        "loop_admits_while_query_stalls: shell._reaper is reaper and statisti"
    )
    started = time.monotonic()

    shell.on_tick(started)
    state = shell._recorder.state
    assert isinstance(state, Querying), "one statistics child owns the query"
    ticket = state.ticket
    assert ticket is not None, "the child has a bounded query ticket"
    child = statistics._children[ticket.serial][1]
    assert child.stdout_open, "the stalled helper retains its output pipe"

    iterations = 0
    for tick in range(1, 11):
        shell.on_tick(started + tick / 10)
        iterations += 1
    assert iterations >= 10, "the loop keeps ticking throughout the pending query"
    assert child.result is None, "the helper remains stalled before its deadline"

    assert world.admission.submit(7), "the supervisor accepts a request mid-query"
    launch = next(
        effect
        for effect in world.admission.token_arrived(b"+")
        if isinstance(effect, Launch)
    )
    ended = world.admission.leader_exited(launch.group_id, Exit(code=0))
    assert ended == [Terminate(launch.group_id)], "the leader exit requests a drain"
    assert world.admission.groups[launch.group_id].state is GroupState.DRAINING, (
        "admission keeps the token held until the scope is empty"
    )
    returned = world.admission.group_emptied(launch.group_id)
    assert returned[0] == ReturnToken(b"+"), (
        "the completed request returns its held token"
    )
    assert not world.admission.groups, "the completed group leaves admission"
    assert world.admission.tokens_held() == 0, "admission no longer holds the token"

    deadline = ticket.deadline
    try:
        shell.on_tick(deadline)
        assert not child.stdout_open, "the timed-out query closes the pipe immediately"
        for _tick in range(20):
            shell.on_tick(deadline + 0.5)
            if child.result is not None:
                break
            time.sleep(0.005)

        assert child.result is not None and child.result.failure == "cancelled", (
            "the timeout cancels the real helper"
        )
        assert child.reaped, "the loop reaps the killed helper"
        assert reaper.active_count == 0, "the reaper releases its helper slot"
        with pytest.raises(ChildProcessError):
            os.waitpid(child._process.pid, os.WNOHANG)
    finally:
        shell.close(timeout=0.1)


def test_blocking_subprocess_negative_control_stops_loop_progress() -> None:
    """Replacing the helper with subprocess.run yields no ticks during its stall."""
    deadline = time.monotonic() + 0.15

    def loop_iterations(start_query: cabc.Callable[[], None]) -> int:
        """Count event-loop turns available before one query deadline."""
        start_query()
        iterations = 0
        while time.monotonic() < deadline:
            iterations += 1
            time.sleep(0.01)
        return iterations

    def blocking_query() -> None:
        """Use the blocking process API as the planned negative control."""
        subprocess.run(
            [sys.executable, "-c", "import time; time.sleep(0.2)"],
            check=True,
            timeout=1,
        )

    assert loop_iterations(blocking_query) == 0, (
        "a synchronous query consumes the full interval before the next tick"
    )


def test_restart_barrier_suppresses_statistics_query(tmp_path: Path) -> None:
    """The outcome gate does not start a query while recovery owns its barrier."""
    barrier = tmp_path / "barrier"
    barrier.touch()
    world = make_outcome_world(
        tmp_path,
        OutcomeWorldOptions(recovery_marker=barrier),
    )

    world.shell.on_tick(0.0)

    assert not world.statistics.starts, "a barrier blocks the statistics child"
    assert world.metrics.events[-1] == (
        "outcome_reading",
        {"gate_reason": "barrier", "readings_skipped": 1},
    ), "the fixed gate reason is observable"
