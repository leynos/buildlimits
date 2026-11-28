"""The producer's decision boundary and its correlation evidence.

The supervisor's launch handler is the first code that runs after admission
granted a token and decided to launch the group. These tests pin where the
recorded decision moment is read, that a later wall-clock read cannot stand in
for it, and that the recorder writes one bounded, path-free correlation
identity per supervisor process.

Every clock is injected: the monotonic clock is a value the test advances, and
the wall clock returns whatever the test sets. That makes the ordering the
tests assert a property of the code rather than of the host's timing.
"""

import datetime as dt
import selectors
import typing as typ
from pathlib import Path

import pytest
from groups_helpers import FakeGroups, refuse_fork
from request_helpers import crate_pending
from supervisor_helpers import supervisor

from buildlimits.config import SupervisorConfig
from buildlimits.groups import Groups
from buildlimits.supervisor_outcome_setup import mint_generation

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.pending_request import Pending
    from buildlimits.supervisor import Supervisor

__all__ = ["supervisor"]  # Re-exported so pytest finds the fixture here.

DECISION_AT = dt.datetime(2026, 11, 24, 9, 0, tzinfo=dt.UTC)
LATER_AT = DECISION_AT + dt.timedelta(minutes=5)


def _admitted_group(supervisor: Supervisor, *, clock: list[float]) -> Pending:
    """Register one request whose group admission already placed.

    Returns
    -------
    Pending
        The admitted request the launch handler will read.
    """
    from buildlimits.admission import Group, GroupState

    supervisor.clock = lambda: clock[0]
    entry = crate_pending(1)
    entry.admitted = None
    entry.admitted_at = None
    supervisor.requests.pending[1] = entry
    supervisor.admission.groups["g1"] = Group(
        "g1", request_id=1, token=b"a", cached=False, state=GroupState.RUNNING
    )
    return entry


def test_a_queue_delay_does_not_move_the_recorded_decision(
    supervisor: Supervisor,
) -> None:
    """The decision is the token grant, not request arrival or queue entry.

    A request can arrive and wait far behind other work. The recorded moment
    must be the one where admission granted the token, so a long queue wait
    never makes a decision look earlier than it was.
    """
    arrived_at = DECISION_AT - dt.timedelta(minutes=30)
    clock = [1800.0]
    supervisor.groups = typ.cast(
        "Groups", FakeGroups(order=[], placed=None, terminate=lambda _g: None)
    )
    supervisor.groups.wall_clock = lambda: DECISION_AT
    entry = _admitted_group(supervisor, clock=clock)
    entry.arrived = 0.0

    supervisor._launch(1, "g1", cached=False)

    assert entry.admitted_at == DECISION_AT, (
        "the decision moment is the token grant, not the arrival time"
    )
    assert entry.admitted == pytest.approx(1800.0), (
        "the monotonic decision time is the grant, not zero"
    )
    assert entry.arrived == pytest.approx(0.0), "the arrival time is unchanged"
    assert entry.admitted_at != arrived_at, "a queue wait cannot shift the decision"


def test_the_decision_moment_is_read_before_any_placement_work(
    supervisor: Supervisor,
) -> None:
    """A clock that advances during placement cannot move the recorded moment."""
    observed: list[tuple[float, dt.datetime]] = []
    clock = [0.0]

    def record_and_advance() -> None:
        """Note both clocks, then move them on as placement would."""
        observed.append((supervisor.clock(), supervisor.groups.wall_clock()))
        clock[0] += 10.0

    supervisor.groups = typ.cast(
        "Groups",
        FakeGroups(order=[], placed=None, on_place=record_and_advance),
    )
    supervisor.groups.wall_clock = lambda: DECISION_AT + dt.timedelta(seconds=clock[0])
    entry = _admitted_group(supervisor, clock=clock)

    supervisor._launch(1, "g1", cached=False)

    assert observed == [(0.0, DECISION_AT)], (
        "both clocks were read at the decision, before placement advanced them"
    )
    assert entry.admitted == pytest.approx(0.0), (
        "the monotonic decision time is the first read"
    )
    assert entry.admitted_at == DECISION_AT, (
        "the UTC decision time is the decision moment, not the later placement"
    )


def test_placement_stamps_only_an_entry_that_has_no_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Placement fills an unmarked entry and leaves a marked one untouched.

    The launch handler marks the decision before placement runs, so a marked
    entry must keep both of its moments even though placement's clocks have
    moved on; a direct caller that marked nothing still gets the pre-execution
    fill. The slice is configured so placement reaches both guards rather than
    refusing earlier, and execution stops at the fork, so the test observes the
    guards' decision and nothing the spawn path might have changed.
    """
    from buildlimits import launch

    def placing(
        clock: cabc.Callable[[], float], wall: cabc.Callable[[], dt.datetime]
    ) -> tuple[Groups, Pending]:
        """Return groups with the slice set and the moment they are given.

        Returns
        -------
        tuple[Groups, Pending]
            The placing groups and a fresh entry.
        """
        groups = Groups(
            SupervisorConfig(), typ.cast("typ.Any", object()), selector, clock
        )
        groups.wall_clock = wall
        groups.slice_dir = Path("/sys/fs/cgroup/compilers.slice")
        return groups, crate_pending(1)

    monkeypatch.setattr(launch, "spawn_held", refuse_fork)
    selector = selectors.DefaultSelector()
    try:
        marked_groups, marked = placing(lambda: 99.0, lambda: LATER_AT)
        marked.admitted = 1.5
        marked.admitted_at = DECISION_AT
        assert marked_groups.place(marked, "g1", cached=False) is None, (
            "the child cannot actually spawn in this test"
        )
        assert marked.admitted == pytest.approx(1.5), (
            "the recorded monotonic decision survives placement"
        )
        assert marked.admitted_at == DECISION_AT, (
            "and so does the recorded UTC moment, not the later clock"
        )

        unmarked_groups, unmarked = placing(lambda: 7.0, lambda: DECISION_AT)
        assert unmarked_groups.place(unmarked, "g2", cached=False) is None, (
            "the child cannot actually spawn in this test"
        )
        assert unmarked.admitted == pytest.approx(7.0), (
            "the monotonic decision time was filled in"
        )
        assert unmarked.admitted_at == DECISION_AT, "and so was the UTC moment"
    finally:
        selector.close()


def test_a_minted_generation_is_bounded_and_path_free() -> None:
    """The supervisor generation is opaque, bounded and distinct per process."""
    first = mint_generation()
    second = mint_generation()

    assert 0 < len(first) <= 64, "the generation fits its recorded bound"
    assert first != second, "a fresh supervisor gets a fresh clock domain"
    assert "/" not in first and " " not in first, "it reveals no path or space"
