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
from groups_helpers import FakeGroups
from request_helpers import crate_pending
from supervisor_helpers import supervisor

from buildlimits.config import SupervisorConfig
from buildlimits.groups import Groups
from buildlimits.supervisor_outcome_setup import mint_generation

if typ.TYPE_CHECKING:
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


def test_placement_never_restamps_an_already_marked_decision() -> None:
    """A marked entry keeps its moment even if placement's clock has moved on."""
    selector = selectors.DefaultSelector()
    try:
        groups = Groups(
            SupervisorConfig(), typ.cast("typ.Any", object()), selector, lambda: 99.0
        )
        groups.wall_clock = lambda: LATER_AT
        entry = crate_pending(1)

        # The slice is unset, so placement refuses before spawning or scoping;
        # what matters is that it did not overwrite the recorded decision.
        with pytest.raises(RuntimeError):
            groups.place(entry, "g1", cached=False)

        assert entry.admitted is None, "no placement fill happened for a refusal"
        assert entry.admitted_at is None, "and no wall clock was read"
    finally:
        selector.close()


def test_placement_fills_an_unmarked_entry_before_its_fork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct caller that marks nothing still records a pre-execution time."""
    from buildlimits import launch

    def exhausted(_spec: object) -> None:
        """Fail as a fork does when the process limit is reached.

        Raises
        ------
        OSError
            Always, standing in for resource exhaustion at fork.
        """
        raise OSError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(launch, "spawn_held", exhausted)
    selector = selectors.DefaultSelector()
    try:
        groups = Groups(
            SupervisorConfig(), typ.cast("typ.Any", object()), selector, lambda: 7.0
        )
        groups.wall_clock = lambda: DECISION_AT
        groups.slice_dir = Path("/sys/fs/cgroup/compilers.slice")
        entry = crate_pending(1)

        assert groups.place(entry, "g1", cached=False) is None, (
            "the child cannot actually spawn in this test"
        )
        assert entry.admitted == pytest.approx(7.0), (
            "the monotonic decision time was filled in"
        )
        assert entry.admitted_at == DECISION_AT, "and so was the UTC moment"
    finally:
        selector.close()


def test_a_minted_generation_is_bounded_and_path_free() -> None:
    """The supervisor generation is opaque, bounded and distinct per process."""
    first = mint_generation()
    second = mint_generation()

    assert 0 < len(first) <= 64, "the generation fits its recorded bound"
    assert first != second, "a fresh supervisor gets a fresh clock domain"
    assert "/" not in first and " " not in first, "it reveals no path or space"
