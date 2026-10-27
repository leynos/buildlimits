"""One driver for the admission properties that sample generated histories.

Two property modules drive the same admission state machine from a generated
operation alphabet: ``test_admission_closing_properties`` samples the
graceful-close invariants, and ``test_kache_admission_properties`` samples the
token accounting of a cache whose work never leaves its group. The machine under
test is the same, so the driver is too; only the invariants asserted after each
step differ.

Sharing the dispatcher keeps the two properties from drifting apart: a new
operation, or a corrected guard, reaches both at once instead of whichever copy
was edited. The assertions stay in their own modules, because those are the
specifications and they are not interchangeable.
"""

import typing as typ

import buildlimits.admission as admission_core
import buildlimits.effects as effect_model


class StepResult(typ.NamedTuple):
    """What one generated operation did.

    Attributes
    ----------
    effects : list[Effect]
        The effects admission returned.
    delivered_token : bool
        Whether the pool actually handed admission a token. A generated
        ``token`` operation with nothing outstanding is not one: the guard
        declines it, and counting it would unbalance the accounting.
    """

    effects: list[effect_model.Effect]
    delivered_token: bool


def first(
    admission: admission_core.Admission, state: admission_core.GroupState
) -> str | None:
    """Pick the oldest group in ``state``, if any.

    Parameters
    ----------
    admission : Admission
        The state to read.
    state : GroupState
        The state a group must be in to be picked.

    Returns
    -------
    str or None
        The group's identifier, or ``None`` when no group is in that state.
    """
    return next(
        (group.group_id for group in admission.groups.values() if group.state is state),
        None,
    )


def first_uncancelled(admission: admission_core.Admission) -> str | None:
    """Pick the oldest group whose requester has not disconnected.

    Parameters
    ----------
    admission : Admission
        The state to read.

    Returns
    -------
    str or None
        The group's identifier, or ``None`` when every group is cancelled.
    """
    return next(
        (group.group_id for group in admission.groups.values() if not group.cancelled),
        None,
    )


def cancel(
    admission: admission_core.Admission, operation: str
) -> list[effect_model.Effect]:
    """Cancel the first matching queued request or running group, if present.

    Parameters
    ----------
    admission : Admission
        The state to change.
    operation : str
        Either ``cancel-queued`` or ``cancel-group``.

    Returns
    -------
    list[Effect]
        The effects of the cancellation, empty when nothing matched.
    """
    if operation == "cancel-queued" and admission.queue:
        return admission.cancel(admission.queue[0])
    group_id = first_uncancelled(admission)
    if operation == "cancel-group" and group_id is not None:
        return admission.cancel(admission.groups[group_id].request_id)
    return []


def step(
    admission: admission_core.Admission, operation: str, sequence_number: int
) -> StepResult:
    """Apply one generated operation, doing nothing where it does not apply.

    Parameters
    ----------
    admission : Admission
        The state to change.
    operation : str
        One name from the property module's generated alphabet.
    sequence_number : int
        The step index, which doubles as the request identifier and the token
        byte so that one generated history cannot alias its own earlier steps.

    Returns
    -------
    StepResult
        The effects of the step and whether a token actually changed hands.
    """
    running_group = first(admission, admission_core.GroupState.RUNNING)
    oldest_group = next(iter(admission.groups), None)
    effects: list[effect_model.Effect] = []
    delivered = False
    match operation:
        case "submit":
            effects = admission.submit(sequence_number)
        case "token" if admission.token_requested:
            effects = admission.token_arrived(bytes([sequence_number]))
            delivered = True
        case "exit" if running_group:
            group = admission.groups[running_group]
            exit_ = (
                effect_model.Exit(signal=15)
                if group.cancelled
                else effect_model.Exit(code=0)
            )
            effects = admission.leader_exited(running_group, exit_)
        case "empty" if oldest_group is not None:
            effects = admission.group_emptied(oldest_group)
        case "cancel-queued" | "cancel-group":
            effects = cancel(admission, operation)
        case "close":
            effects = admission.begin_closing()
        case "fail" if running_group:
            effects = admission.launch_failed(running_group)
    return StepResult(effects, delivered)
