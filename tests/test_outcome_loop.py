"""Outcome polling stays non-blocking under the supervisor's loop cadence."""

import os
import sys
import threading
import time
import typing as typ
from pathlib import Path

import pytest
from outcome_shell_test_support import (
    OutcomeWorld,
    OutcomeWorldOptions,
    PendingStatistics,
    make_outcome_world,
)
from outcome_test_helpers import stats_text

from buildlimits.admission import Launch
from buildlimits.admission_state import GroupState
from buildlimits.effects import Exit, ReturnToken, Terminate
from buildlimits.nonblocking_child import ChildReaper, NonBlockingChild
from buildlimits.outcome_ports import StatisticsText, Ticket
from buildlimits.outcome_queries import SccacheStatistics
from buildlimits.outcome_recorder_types import Disabled, Querying

if typ.TYPE_CHECKING:
    from buildlimits.supervisor_outcomes import OutcomeShell
    from buildlimits.tracing import JournalTracer


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

    try:
        _assert_timeout_cancels(shell, child, reaper, ticket)
    finally:
        shell.close(timeout=0.1)


def _assert_timeout_cancels(
    shell: OutcomeShell,
    child: NonBlockingChild,
    reaper: ChildReaper,
    ticket: Ticket,
) -> None:
    """Assert the deadline closes the pipe, cancels, reaps and frees the slot."""
    deadline = ticket.deadline
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


class _ImmediateStatistics(PendingStatistics):
    """Return one complete reading so idle query scheduling can be measured."""

    def __init__(self, text: str = stats_text()) -> None:
        """Configure the shared query fake to return complete statistics."""
        super().__init__(StatisticsText(text))


class _BlockingStatistics(_ImmediateStatistics):
    """Delay query creation to model an incorrectly synchronous adapter."""

    def start(self, now: float) -> Ticket:
        """Block longer than one loop interval before returning the ticket."""
        threading.Event().wait(0.2)
        return super().start(now)


class _RecordingSpan:
    """Accept terminal tracing fields from one selection span."""

    @staticmethod
    def finish(**_fields: object) -> None:
        """Consume the bounded terminal classification."""


class _RecordingTracer:
    """Retain fixed span names without initializing the tracing SDK."""

    def __init__(self) -> None:
        """Start with no recorded operations."""
        self.started: dict[str, int] = {}

    def start(self, operation: str) -> _RecordingSpan:
        """Record the requested operation and return a disposable span."""
        self.started[operation] = self.started.get(operation, 0) + 1
        return _RecordingSpan()


def _make_traced_world(
    tmp_path: Path, statistics: _ImmediateStatistics
) -> tuple[OutcomeWorld, OutcomeShell, _RecordingTracer]:
    """Build a real outcome shell with a deterministic tracer.

    Example:
        world, shell, tracer = _make_traced_world(tmp_path, _ImmediateStatistics())

    Returns
    -------
    tuple[OutcomeWorld, OutcomeShell, _RecordingTracer]
        The world, its production shell and the tracer's recorded spans.
    """
    tracer = _RecordingTracer()
    world = make_outcome_world(
        tmp_path,
        OutcomeWorldOptions(
            statistics_port=statistics,
            tracer=typ.cast("JournalTracer", tracer),
        ),
    )
    return world, typ.cast("OutcomeShell", world.shell), tracer


def test_idle_baseline_does_not_repeat_statistics_or_selection(
    tmp_path: Path,
) -> None:
    """An idle cache does not keep opening an empty statistics window."""
    statistics = _ImmediateStatistics()
    world, shell, tracer = _make_traced_world(tmp_path, statistics)

    shell.on_tick(0.0)
    shell.on_tick(0.1)
    assert shell._recorder.state.progress.baseline is not None, (
        "the initial reading establishes the baseline"
    )
    selection_count = tracer.started.get("admission_selection", 0)
    reading_count = len(world.metrics.events)

    for tick in range(2, 62):
        shell.on_tick(float(tick))

    assert len(statistics.starts) == 1, "idle ticks do not start more statistics reads"
    assert tracer.started.get("admission_selection", 0) == selection_count, (
        "idle ticks do not open selection spans"
    )
    assert len(world.metrics.events) == reading_count, (
        "idle ticks do not emit skipped-reading metrics"
    )


def test_disabled_recorder_stops_sampling_and_reports_disabled(
    tmp_path: Path,
) -> None:
    """An unsupported reading disables further gate and sampling work."""
    statistics = _ImmediateStatistics('{"version":"9.9.9","stats":{}}')
    world, shell, tracer = _make_traced_world(tmp_path, statistics)

    shell.on_tick(0.0)
    shell.on_tick(0.1)
    assert isinstance(shell._recorder.state, Disabled), (
        "an unsupported statistics version disables recording"
    )
    assert shell.describe()["enabled"] is False, "status reflects the disabled recorder"
    selections = tracer.started.get("admission_selection", 0)
    barrier = tmp_path / "barrier"
    barrier.touch()

    shell.on_tick(2.0)
    shell.on_tick(3.0)

    assert tracer.started.get("admission_selection", 0) == selections, (
        "a disabled recorder does not open selection spans"
    )
    assert not any(
        fields.get("gate_reason") == "barrier" for _, fields in world.metrics.events
    ), "a disabled recorder does not emit skipped-reading metrics"


def test_blocking_statistics_port_stops_shell_tick_progress(tmp_path: Path) -> None:
    """A blocking statistics start stalls actual shell progress as a negative control."""
    statistics = _BlockingStatistics()
    world = make_outcome_world(
        tmp_path,
        OutcomeWorldOptions(statistics_port=statistics),
    )
    shell = typ.cast("OutcomeShell", world.shell)
    deadline = time.monotonic() + 0.15

    shell.on_tick(0.0)
    ticks = 0
    while time.monotonic() < deadline:
        ticks += 1
        shell.on_tick(float(ticks) / 10)

    assert ticks == 0, "the synchronous port consumed the shell's tick interval"


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
