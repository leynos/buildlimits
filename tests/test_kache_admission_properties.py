"""Token accounting for a cache whose work never leaves the group.

sccache accepts a request and runs the miss outside the group, so an emptied
scope is not proof that its work is over and the token waits in
``SERVER_DRAIN`` until the server is accounted for. kache composes the compiler
as a child of the wrapper inside the group, so an empty scope *is* the proof:
the token can go back the moment the scope empties, whatever the leader's exit
looked like.

The property sampled here is that whole accounting rule over the operation
alphabet admission itself offers, rather than over a scripted happy path. After
every step three things must hold:

- no group is in ``SERVER_DRAIN``, because an in-scope cache cannot leave work
  for a server to finish;
- every group that left admission took its own token back, exactly once, in the
  same step; and
- the tokens admission has taken in less the tokens it has returned is exactly
  the number of groups it still holds, so neither a leak nor a double return
  can survive a step even when no single effect looked wrong.
"""

import typing as typ

import hypothesis
from admission_operations import step
from hypothesis import strategies

import buildlimits.admission as admission_core
import buildlimits.effects as effect_model

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: The operations admission offers, in the vocabulary the closing-property
#: module already uses, so a reader compares the two directly.
OPERATIONS = (
    "submit",
    "token",
    "exit",
    "empty",
    "cancel-queued",
    "cancel-group",
    "fail",
)


def _returns(effects: list[effect_model.Effect]) -> list[bytes]:
    """List the tokens a step returned."""
    return [
        effect.token
        for effect in effects
        if isinstance(effect, effect_model.ReturnToken)
    ]


def _assert_no_server_drain(admission: admission_core.Admission) -> None:
    """No kache group may wait for a server that cannot be running its work."""
    stranded = [
        group_id
        for group_id, group in admission.groups.items()
        if group.state is admission_core.GroupState.SERVER_DRAIN
    ]
    assert not stranded, (
        f"an in-scope cache group reached SERVER_DRAIN: {stranded}; "
        "its token would be held for work that cannot exist"
    )


def _assert_every_departure_returns_its_token(
    before: cabc.Mapping[str, bytes],
    after: cabc.Mapping[str, bytes],
    returned: list[bytes],
) -> None:
    """Check that each group which left admission took its token back once."""
    for group_id, token in before.items():
        if group_id in after:
            continue
        assert token in returned, (
            f"{group_id} left admission without returning its token"
        )
        assert returned.count(token) == 1, (
            f"{group_id} returned its token more than once"
        )
    for token in returned:
        assert token not in after.values(), (
            "a held group's token was returned while the group still runs"
        )


def _assert_balance(
    *,
    taken: int,
    returned: list[bytes],
    admission: admission_core.Admission,
) -> None:
    """Everything taken in is held by a group or has been returned."""
    assert taken - len(returned) == len(admission.groups), (
        f"{taken} token(s) taken in, {len(returned)} returned, "
        f"{len(admission.groups)} group(s) held; the difference is a leak or a "
        f"double return. Groups: {sorted(admission.groups)}"
    )


@hypothesis.settings(max_examples=200, deadline=None)
@hypothesis.example(operations=["submit", "token", "empty"])
@hypothesis.example(operations=["submit", "token", "exit", "empty"])
@hypothesis.example(operations=["submit", "token", "cancel-group", "exit", "empty"])
@hypothesis.example(operations=["submit", "token", "submit", "token", "empty", "empty"])
@hypothesis.given(
    operations=strategies.lists(strategies.sampled_from(OPERATIONS), max_size=40)
)
def test_a_kache_group_returns_its_token_when_its_scope_empties(
    operations: list[str],
) -> None:
    """Token accounting holds over every generated kache admission history."""
    admission = admission_core.Admission(cached=True, cache_detached=False)
    taken = 0
    returned: list[bytes] = []
    for sequence_number, operation in enumerate(operations, start=1):
        before = {group_id: group.token for group_id, group in admission.groups.items()}
        result = step(admission, operation, sequence_number)
        taken += int(result.delivered_token)
        step_returns = _returns(result.effects)
        returned.extend(step_returns)
        after = {group_id: group.token for group_id, group in admission.groups.items()}
        _assert_no_server_drain(admission)
        _assert_every_departure_returns_its_token(before, after, step_returns)
        _assert_balance(taken=taken, returned=returned, admission=admission)


def test_a_cached_kache_group_is_composed_cached_and_never_stranded() -> None:
    """The sampled property is not vacuous: a kache group is composed cached.

    Without this, a generator that never produced a cached group would satisfy
    every assertion above while exercising none of them.
    """
    admission = admission_core.Admission(cached=True, cache_detached=False)
    admission.submit(1)
    [launch] = [
        effect
        for effect in admission.token_arrived(b"+")
        if isinstance(effect, effect_model.Launch)
    ]
    assert launch.cached, "a cacheable request in kache mode is composed cached"
    group = admission.groups[launch.group_id]
    assert not group.detached, "an in-scope backend's groups are never detached"
    admission.leader_exited(launch.group_id, effect_model.Exit(code=0))
    effects = admission.group_emptied(launch.group_id)
    assert effect_model.ReturnToken(b"+") in effects, effects
    assert launch.group_id not in admission.groups, "the emptied group is forgotten"
