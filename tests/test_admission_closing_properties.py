"""Sample admission histories to check graceful-close invariants.

The companion ``test_admission_closing`` module keeps focused examples of
individual transitions. This module checks those transitions in combination.
"""

import typing as typ

import hypothesis
from admission_operations import first, first_uncancelled, step
from hypothesis import strategies

import buildlimits.admission as admission_core
import buildlimits.effects as effect_model


class _Before(typ.NamedTuple):
    """Admission facts captured before one generated operation."""

    operation: str
    graceful_close_requested: bool
    running: tuple[str, int, bytes] | None
    unrelated_groups: dict[
        str,
        tuple[
            int,
            bytes,
            bool,
            admission_core.GroupState,
            bool,
            effect_model.Exit | None,
        ],
    ]
    queued: list[int]
    cancellation: tuple[str, bool] | None


def _group_snapshot(
    admission: admission_core.Admission, *, excluding: str | None = None
) -> dict[
    str,
    tuple[
        int,
        bytes,
        bool,
        admission_core.GroupState,
        bool,
        effect_model.Exit | None,
    ],
]:
    """Copy the state fields used to detect unrelated group changes."""
    return {
        group_id: (
            group.request_id,
            group.token,
            group.cached,
            group.state,
            group.cancelled,
            group.exit,
        )
        for group_id, group in admission.groups.items()
        if group_id != excluding
    }


def _snapshot(
    admission: admission_core.Admission,
    operation: str,
    graceful_close_requested: bool,
) -> _Before:
    """Record the independent model inputs needed to check this transition."""
    running_group = first(admission, admission_core.GroupState.RUNNING)
    cancellation_group = (
        first_uncancelled(admission) if operation == "cancel-group" else None
    )
    target = admission.groups.get(running_group) if running_group else None
    cancellation = (
        admission.groups.get(cancellation_group) if cancellation_group else None
    )
    return _Before(
        operation,
        graceful_close_requested,
        None
        if target is None or running_group is None
        else (running_group, target.request_id, target.token),
        _group_snapshot(admission, excluding=running_group),
        list(admission.queue),
        None
        if cancellation is None or cancellation_group is None
        else (
            cancellation_group,
            cancellation.state is admission_core.GroupState.RUNNING,
        ),
    )


def _assert_failed_placement(
    admission: admission_core.Admission,
    effects: list[effect_model.Effect],
    before: _Before,
) -> None:
    """Check the failed group's exact refusal and token accounting."""
    running = before.running
    if before.operation != "fail" or running is None:
        return
    running_group, owner, token = running
    expected_reason = (
        "restarting" if before.graceful_close_requested else "launch-failed"
    )
    refusals = [
        effect
        for effect in effects
        if isinstance(effect, admission_core.Refuse) and effect.request_id == owner
    ]
    assert refusals == [admission_core.Refuse(owner, expected_reason)], (
        "the failed request receives exactly its expected refusal"
    )
    returned_tokens = [
        effect for effect in effects if isinstance(effect, admission_core.ReturnToken)
    ]
    assert returned_tokens == [admission_core.ReturnToken(token)], (
        "the failed group's own token returns exactly once"
    )
    assert running_group not in admission.groups, "the failed group is forgotten"
    assert _group_snapshot(admission) == before.unrelated_groups, (
        "a failed placement does not change another group's state"
    )


def _assert_transition_effects(
    admission: admission_core.Admission,
    effects: list[effect_model.Effect],
    before: _Before,
) -> None:
    """Check health, close intent, launches and termination causes."""
    graceful_close_requested = before.graceful_close_requested or (
        before.operation == "close"
    )
    assert admission.healthy, "this generator excludes health failures"
    assert admission.closing is graceful_close_requested, (
        "the independent model tracks the graceful-close transition"
    )
    if graceful_close_requested:
        assert not any(
            isinstance(e, (admission_core.Launch, admission_core.RequestToken))
            for e in effects
        ), "a closing admission launches nothing and requests no token"

    terminations = [
        effect for effect in effects if isinstance(effect, admission_core.Terminate)
    ]
    expected: list[admission_core.Terminate] = []
    match (before.operation, before.running, before.cancellation):
        case ("exit", (group_id, _, _), _):
            expected = [admission_core.Terminate(group_id)]
        case ("cancel-group", _, (group_id, True)):
            expected = [admission_core.Terminate(group_id)]
    assert terminations == expected, (
        "only a leader exit or requester cancellation terminates a group"
    )


def _assert_shutdown_effects(
    admission: admission_core.Admission,
    effects: list[effect_model.Effect],
    before: _Before,
    shutdowns: int,
) -> int:
    """Check exact-once shutdown and refusal of the entire waiting queue."""
    graceful_close_requested = before.graceful_close_requested or (
        before.operation == "close"
    )
    emitted = sum(isinstance(effect, admission_core.Shutdown) for effect in effects)
    shutdowns += emitted
    expected = int(graceful_close_requested and not admission.groups)
    assert shutdowns == expected, (
        "shutdown occurs once, exactly when closing has no group to await"
    )
    if emitted:
        queued_refusals = [
            effect
            for effect in effects
            if isinstance(effect, admission_core.Refuse)
            and effect.request_id in before.queued
        ]
        assert queued_refusals == [
            admission_core.Refuse(request_id, "restarting")
            for request_id in before.queued
        ], "every queued request is refused once"
        shutdown_index = next(
            index
            for index, effect in enumerate(effects)
            if isinstance(effect, admission_core.Shutdown)
        )
        refusal_indices = [
            index
            for index, effect in enumerate(effects)
            if isinstance(effect, admission_core.Refuse)
            and effect.request_id in before.queued
        ]
        assert all(index < shutdown_index for index in refusal_indices), (
            "queued requests are refused before shutdown"
        )
        assert not admission.queue, "shutdown leaves no queued request behind"
    assert not (
        graceful_close_requested and not admission.groups and admission.queue
    ), "a drained closing admission has no queued request"
    return shutdowns


@hypothesis.settings(max_examples=200, deadline=None)
# Named histories exercise both failure classifications and multi-group drains.
@hypothesis.example(operations=["submit", "token", "close", "fail"])
@hypothesis.example(operations=["submit", "token", "fail", "close"])
@hypothesis.example(operations=["close", "close"])
@hypothesis.example(
    operations=[
        "submit",
        "token",
        "submit",
        "token",
        "submit",
        "close",
        "token",
        "fail",
        "exit",
        "empty",
    ]
)
@hypothesis.example(operations=["submit", "token", "submit", "close", "empty"])
@hypothesis.example(
    operations=["submit", "token", "close", "cancel-group", "exit", "empty"]
)
@hypothesis.given(
    operations=strategies.lists(
        strategies.sampled_from([
            "submit",
            "token",
            "exit",
            "empty",
            "close",
            "cancel-queued",
            "cancel-group",
            "fail",
        ]),
        max_size=40,
    )
)
def test_graceful_close_preserves_admission_invariants(operations: list[str]) -> None:
    """Sample healthy, uncached admission histories around graceful closing.

    Closing itself launches and terminates nothing. A separate requester
    cancellation may terminate its group, and a leader exit may terminate
    leftovers. ``fail`` models placement failure before a compiler starts.
    Cached server drain, health failure, stop signals and the process execution
    phase are outside this admission-level model.
    """
    admission = admission_core.Admission()
    graceful_close_requested = False
    shutdowns = 0
    for sequence_number, operation in enumerate(operations, start=1):
        before = _snapshot(admission, operation, graceful_close_requested)
        effects = step(admission, operation, sequence_number).effects
        if operation == "close":
            graceful_close_requested = True

        _assert_transition_effects(admission, effects, before)
        _assert_failed_placement(admission, effects, before)
        shutdowns = _assert_shutdown_effects(admission, effects, before, shutdowns)
