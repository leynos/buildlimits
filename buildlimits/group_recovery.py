"""Recover lost control effects without inferring compiler completion.

Only the supervisor uses these operations. Admission remains the token owner;
the OS group port retains processes until their scopes are verified empty.
"""

import typing as typ

from buildlimits.admission import GroupState

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.admission import Admission, Effect
    from buildlimits.groups import Groups


def reap_leaders(
    groups: Groups,
    admission: Admission,
    diagnose: cabc.Callable[[str], None],
    apply: cabc.Callable[[list[Effect]], None],
) -> None:
    """Account for every consumed exit before applying fallible diagnostics.

    Parameters
    ----------
    groups : Groups
        OS group ownership and leader wait statuses.
    admission : Admission
        Token owner receiving every observed exit.
    diagnose : Callable[[str], None]
        Read the group's bounded setup diagnostic.
    apply : Callable[[list[Effect]], None]
        Schedule scope termination after the exit is recorded.

    Raises
    ------
    Exception
        The first diagnostic or effect failure, after processing the remaining
        observed exits. Admission still retains each token until scope emptiness.
    """  # ruff: ignore[docstring-extraneous-exception] - re-raises the captured port exception.
    ended = [
        (running.group_id, admission.leader_exited(running.group_id, exit_))
        for running, exit_ in groups.reap()
    ]
    failure: Exception | None = None
    for group_id, effects in ended:
        try:
            try:
                diagnose(group_id)
            finally:
                apply(effects)
        except Exception as error:  # ruff: ignore[blind-except] - process all consumed exits before surfacing the first fault.
            failure = failure or error
    if failure is not None:
        raise failure


def retry_termination(groups: Groups, admission: Admission) -> None:
    """Reconcile lost termination effects for cancelled or draining scopes.

    Parameters
    ----------
    groups : Groups
        OS process groups and their escalation deadlines.
    admission : Admission
        Explicit cancellation and draining state. Ordinary running groups and
        cached server-drain tokens never authorize termination here.
    """
    for group in admission.groups.values():
        if group.state not in {GroupState.RUNNING, GroupState.DRAINING}:
            continue
        running = groups.running.get(group.group_id)
        if running is None or running.kill_at is not None:
            continue
        if group.state is GroupState.DRAINING or group.cancelled:
            groups.terminate(group.group_id)
