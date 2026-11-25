"""Clause-order tests for the pure quiet-point gate."""

import dataclasses as dc

import pytest

from buildlimits.admission_state import Counts
from buildlimits.outcome_gate import QuietInputs, quiet_point


@pytest.fixture
def ready_inputs() -> QuietInputs:
    """Return a quiet snapshot with irrelevant uncached work still present."""
    return QuietInputs(
        counts=Counts(
            queued=0,
            held=4,
            running=2,
            draining=1,
            server_drain=0,
            capacity=8,
            cached_moving=0,
        ),
        is_reachable=True,
        is_cache_allowed=True,
        is_recovery_idle=True,
        has_barrier=False,
        has_query_in_flight=False,
    )


def test_all_quiet_point_clauses_allow_a_reading(ready_inputs: QuietInputs) -> None:
    """Accept the reading when all outcome gates are clear."""
    assert quiet_point(ready_inputs) is None, "all clear gate clauses permit a reading"


def test_queued_work_does_not_block_a_reading(ready_inputs: QuietInputs) -> None:
    """A deep queue is not a gate: a straddling launch is refused separately."""
    busy_queue = dc.replace(
        ready_inputs, counts=dc.replace(ready_inputs.counts, queued=647)
    )

    assert quiet_point(busy_queue) is None, (
        "queued work may run while no cached group is moving"
    )


def test_a_boundary_is_not_read_while_cached_work_moves(
    ready_inputs: QuietInputs,
) -> None:
    """A baseline holds until no cached compilation can still move a counter.

    A launch in flight when the boundary is read adds to the next window's
    delta without appearing in that window, so its label would be lost or
    misread. No reading, baseline included, may start while one is moving.
    """
    moving = dc.replace(
        ready_inputs,
        counts=dc.replace(
            ready_inputs.counts,
            queued=647,
            cached_moving=3,
            server_drain=1,
        ),
    )

    assert quiet_point(moving) == "cached_moving", (
        "a moving cached group blocks the boundary"
    )


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"counts": Counts(0, 1, 1, 0, 0, 1, 1)}, "cached_moving"),
        ({"counts": Counts(0, 1, 0, 1, 1, 1, 0)}, "server_drain"),
        ({"is_reachable": False}, "unreachable"),
        ({"is_cache_allowed": False}, "held"),
        ({"is_recovery_idle": False}, "recovering"),
        ({"has_barrier": True}, "barrier"),
        ({"has_query_in_flight": True}, "in_flight"),
    ],
    ids=(
        "cached-moving",
        "server-drain",
        "unreachable",
        "held",
        "recovering",
        "barrier",
        "in-flight",
    ),
)
def test_each_false_clause_blocks_a_reading(
    ready_inputs: QuietInputs, change: dict[str, object], reason: str
) -> None:
    """Return the reason for the single false quiet-point clause."""
    changed = dc.replace(ready_inputs, **change)

    assert quiet_point(changed) == reason, (
        "the single false gate clause blocks the reading"
    )


def test_first_false_clause_wins(ready_inputs: QuietInputs) -> None:
    """Keep the documented clause order when several gates are closed."""
    blocked = dc.replace(
        ready_inputs,
        counts=Counts(
            queued=1,
            held=0,
            running=0,
            draining=0,
            server_drain=1,
            capacity=1,
            cached_moving=1,
        ),
        is_reachable=False,
        is_cache_allowed=False,
        is_recovery_idle=False,
        has_barrier=True,
        has_query_in_flight=True,
    )

    assert quiet_point(blocked) == "cached_moving", (
        "the earliest remaining false clause takes precedence"
    )
