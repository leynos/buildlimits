"""Place admitted compiler groups and record bounded scope outcomes."""

import typing as typ

from buildlimits import effect_dispatch, kache_guard, supervisor_shutdown
from buildlimits.journal import admitted_line
from buildlimits.protocol import Admitted

if typ.TYPE_CHECKING:
    from buildlimits.pending_request import Pending
    from buildlimits.supervisor import Supervisor


def launch(
    supervisor: Supervisor, request_id: int, group_id: str, *, cached: bool
) -> None:
    """Start an admitted compiler inside its transient systemd scope."""
    span = (
        None
        if supervisor.tracer is None
        else supervisor.tracer.start("scope_placement")
    )
    outcome = "failed"
    failure_category = "internal_error"
    stop_mode = placement_stop_mode(supervisor)
    try:
        entry = supervisor.requests.pending[request_id]
        # This handler is the first code that runs after admission decided to
        # grant the token and launch the group. Read both clocks here, before
        # any placement work, so the recorded decision boundary is the moment
        # the decision was applied rather than a later execution timestamp.
        mark_admission(supervisor, entry)
        # Read the slice before placing this group, so the reading describes
        # the state the group is admitted into rather than one that already
        # includes it. The poll alone would leave up to a poll interval in
        # which a stray from an earlier group is invisible; admission is when
        # the supervisor is about to make the same promise again.
        kache_guard.refresh_guard(supervisor.groups)
        running = supervisor.groups.place(entry, group_id, cached=cached)
        if running is None:
            stop_mode = placement_stop_mode(supervisor)
            supervisor_shutdown.fail_placement(supervisor, entry, group_id)
            stop_mode = placement_stop_mode(supervisor, stop_mode)
            outcome = "restarting" if stop_mode == "graceful" else "launch_failed"
            failure_category = "scope_placement"
            return
        # The requester learns of admission before the compiler can write.
        notice = supervisor.groups.cache_notice(entry, cached=cached)
        effect_dispatch.record_admitted(supervisor, entry, cached=cached)
        supervisor.requests.notify(request_id, Admitted(group_id, cached, notice))
        if entry.conn is None:
            supervisor.groups.abandon_held(running)
            outcome = "requester_gone"
            failure_category = "requester_unavailable"
        elif supervisor.groups.release(running) is False:
            outcome = "release_failed"
            failure_category = "launch_release"
        else:
            supervisor.routine_lines.write(
                admitted_line(entry, cached=cached, cache_name=supervisor.backend.name),
                routine=True,
            )
            supervisor.outcomes.launched(entry, group_id)
            outcome = "admitted"
            failure_category = "none"
            return
        entry.exec_error = running.exec_error
        supervisor._apply(supervisor.admission.launch_failed(group_id))
    finally:
        if span is not None:
            span.finish(
                outcome=outcome,
                failure_category=failure_category,
                stop_mode=placement_stop_mode(supervisor, stop_mode),
            )


def mark_admission(supervisor: Supervisor, entry: Pending) -> None:
    """Capture the one moment the admission decision is applied.

    The monotonic and UTC readings are taken together, before any placement
    work such as the stray-process guard or the fork. A later reading would
    timestamp process launch rather than the decision an offline predictor must
    reproduce. Direct callers of :meth:`Groups.place` that have not marked an
    admission keep their previous behaviour: placement fills both values only
    when they are absent.

    Parameters
    ----------
    supervisor : Supervisor
        Owner of the monotonic clock and the placement wall clock.
    entry : Pending
        The admitted request whose decision moment is being recorded.
    """
    entry.admitted = supervisor.clock()
    entry.admitted_at = supervisor.groups.wall_clock()


def placement_stop_mode(supervisor: Supervisor, previous: str = "none") -> str:
    """Classify only fixed stop state for the scope-placement span.

    Returns
    -------
    str
        One of ``none``, ``graceful`` or ``immediate``.
    """
    if previous == "immediate" or supervisor._stop_requested:
        return "immediate"
    if previous == "graceful":
        return "graceful"
    if supervisor._graceful_stop_requested or supervisor.admission.closing:
        return "graceful"
    return "none"


def exec_outcome(supervisor: Supervisor, group_id: str) -> None:
    """Record why a released child could not execute its compiler."""
    outcome = supervisor.groups.exec_outcome(group_id)
    if outcome is None:
        return
    request_id, message = outcome
    entry = supervisor.requests.pending.get(request_id)
    if entry is not None:
        entry.exec_error = message
