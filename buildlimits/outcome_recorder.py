"""Pure state transitions for quiet-point cache-outcome readings."""

import dataclasses as dc

from buildlimits.outcome_gate import quiet_point
from buildlimits.outcome_ports import Pending, QueryFailure, StatisticsText, Ticket
from buildlimits.outcome_recorder_outputs import (
    _emit_unattributed,
    _join_outputs,
    _with_progress,
)
from buildlimits.outcome_recorder_reading import _finish_failure, _finish_reading
from buildlimits.outcome_recorder_sampling import sampled as _sampled
from buildlimits.outcome_recorder_types import (
    CloseQuery,
    Disabled,
    Ended,
    GateOpen,
    Idle,
    Launched,
    Outputs,
    PollResult,
    QueryFinished,
    Querying,
    QueryStarted,
    RecorderEvent,
    RecorderState,
    Sampled,
    StartQuery,
    Stopping,
    _Launch,
)
from buildlimits.outcome_recorder_types import (
    LaunchFailed as LaunchFailed,
)
from buildlimits.outcome_recorder_types import (
    LaunchPayload as LaunchPayload,
)
from buildlimits.outcome_recorder_types import (
    Progress as Progress,
)
from buildlimits.outcome_recorder_types import (
    SampleFailed as SampleFailed,
)
from buildlimits.outcome_sightings import ChildSightings
from buildlimits.outcome_stats import ReadFailure, parse_statistics
from buildlimits.outcome_window import OutcomeWindow

OUTCOME_QUERY_SECONDS = 2.0
SUPPORTED_SCCACHE_VERSIONS = frozenset({"0.14.0"})


def advance(
    state: RecorderState, event: RecorderEvent
) -> tuple[RecorderState, Outputs]:
    """Apply one event without I/O, logging, or reading a clock.

    Returns
    -------
    tuple[RecorderState, Outputs]
        The next typestate and all effects for the imperative shell.

    Examples
    --------
    ``advance(Idle(), LaunchFailed("g"))[1]`` is ``Outputs()``.
    """
    if isinstance(event, (Stopping, Launched, Ended, GateOpen, PollResult, Sampled)):
        return _advance_enabled(state, event)
    return state, Outputs()


def _advance_enabled(
    state: RecorderState,
    event: Stopping | Launched | Ended | GateOpen | PollResult | Sampled,
) -> tuple[RecorderState, Outputs]:
    """Dispatch events that can change recorder state or emit effects.

    Returns
    -------
    tuple[RecorderState, Outputs]
        The next typestate and its commands, records, or tracing effects.
    """
    match event:
        case Stopping():
            return _stop(state, event)
        case Launched():
            return _launch(state, event)
        case Ended():
            return _ended(state, event)
        case GateOpen():
            return _open_gate(state, event)
        case PollResult():
            return _poll_result(state, event)
        case Sampled():
            return _sample(state, event)


def _launch(state: RecorderState, event: Launched) -> tuple[RecorderState, Outputs]:
    """Retain cached metadata and mark any query interval as straddled.

    Returns
    -------
    tuple[RecorderState, Outputs]
        The updated progress and any overflow effects.

    Examples
    --------
    An uncached launch leaves the recorder state unchanged.
    """
    if not event.is_cached or isinstance(state, Disabled):
        return state, Outputs()

    launch = _Launch(
        event.group_id,
        event.payload,
        event.now,
        sightings=ChildSightings(has_metadata=event.payload.metadata is not None),
    )
    progress = state.progress
    window = progress.window.append(launch)
    active = (*progress.active, launch)
    progress = dc.replace(progress, active=active)
    if window.is_overflowed:
        overflowed = (*window.entries, launch)
        outputs = _emit_unattributed(
            overflowed,
            "window_overflow",
            event.now,
            None,
        )
        ids = {item.group_id for item in overflowed}
        progress = dc.replace(
            progress,
            active=tuple(item for item in active if item.group_id not in ids),
            window=OutcomeWindow(),
        )
        next_state = _with_progress(state, progress, straddled=True)
        return next_state, outputs

    progress = dc.replace(progress, window=window)
    return _with_progress(state, progress, straddled=True), Outputs()


def _ended(state: RecorderState, event: Ended) -> tuple[RecorderState, Outputs]:
    """Record runtime and cleanliness for one active group.

    Returns
    -------
    tuple[RecorderState, Outputs]
        The updated launch metadata, or unchanged state for an unknown group.

    Examples
    --------
    Ending an unknown group returns empty output.
    """
    if isinstance(state, Disabled):
        return state, Outputs()
    active = next(
        (item for item in state.progress.active if item.group_id == event.group_id),
        None,
    )
    if active is None or active.runtime_seconds is not None:
        return state, Outputs()
    ended = dc.replace(
        active,
        runtime_seconds=max(0.0, event.now - active.started_at),
        is_clean=event.is_clean,
        observed_at=event.observed_at,
    )
    progress = dc.replace(
        state.progress,
        active=tuple(
            ended if item.group_id == event.group_id else item
            for item in state.progress.active
        ),
        window=dc.replace(
            state.progress.window,
            entries=tuple(
                ended if item.group_id == event.group_id else item
                for item in state.progress.window.entries
            ),
        ),
    )
    return _with_progress(state, progress), Outputs()


def _sample(state: RecorderState, event: Sampled) -> tuple[RecorderState, Outputs]:
    """Fold one child sample into running launches, never into ended ones.

    Returns
    -------
    tuple[RecorderState, Outputs]
        Updated active sightings and no shell effects.
    """
    if isinstance(state, Disabled):
        return state, Outputs()
    return _with_progress(state, _sampled(state.progress, event.metadata)), Outputs()


def _open_gate(state: RecorderState, event: GateOpen) -> tuple[RecorderState, Outputs]:
    """Start at most one due query when all quiet-point clauses hold.

    Returns
    -------
    tuple[RecorderState, Outputs]
        The querying state and one start command, or the unchanged state.

    Examples
    --------
    A blocked quiet point emits no query command.
    """
    if not isinstance(state, Idle):
        return state, Outputs()
    progress = state.progress
    if event.now < progress.next_read_at:
        return state, Outputs()
    if quiet_point(event.inputs) is not None:
        return state, Outputs()

    # GateOpen is the scheduler's approval that the read is due; this reducer
    # owns quiet-point safety and in-flight exclusion, not polling cadence.
    querying = Querying(progress=progress, started_at=event.now)
    outputs = Outputs(
        commands=(StartQuery(event.now),),
        spans=(QueryStarted(event.now),),
    )
    return querying, outputs


def _poll_result(
    state: RecorderState, event: PollResult
) -> tuple[RecorderState, Outputs]:
    """Accept a ticket, retain pending work, or close a terminal reading.

    Returns
    -------
    tuple[RecorderState, Outputs]
        The updated query state and effects for one port response.

    Examples
    --------
    An unrelated poll result leaves a non-querying state unchanged.
    """
    if not isinstance(state, Querying):
        return state, Outputs()
    if isinstance(event.result, Ticket):
        if state.ticket is not None:
            return state, Outputs()
        before = event.before_generation
        return dc.replace(
            state,
            ticket=event.result,
            before_generation=before,
        ), Outputs()
    if state.ticket is not None and event.now >= min(
        state.ticket.deadline,
        state.started_at + OUTCOME_QUERY_SECONDS,
    ):
        return _finish_failure(state, event.now, "timeout")
    return _complete_result(state, event)


def _complete_result(
    state: Querying, event: PollResult
) -> tuple[RecorderState, Outputs]:
    """Interpret one terminal, pending or malformed port result.

    Returns
    -------
    tuple[RecorderState, Outputs]
        The next state and effects for the supplied query result.

    Examples
    --------
    A pending ticket retains the querying state without output.
    """
    match event.result:
        case Pending() if state.ticket is not None:
            return state, Outputs()
        case QueryFailure(reason=reason):
            return _finish_failure(state, event.now, reason)
        case StatisticsText(text=text):
            parsed = parse_statistics(text, supported=SUPPORTED_SCCACHE_VERSIONS)
            if isinstance(parsed, ReadFailure):
                if parsed.reason == "unsupported_version":
                    return _finish_failure(
                        state, event.now, parsed.reason, disable=True
                    )
                return _finish_failure(state, event.now, parsed.reason)
            return _finish_reading(state, event, parsed)
        case _:
            return state, Outputs()


def _stop(state: RecorderState, event: Stopping) -> tuple[RecorderState, Outputs]:
    """Close a ticket and account for every launch at graceful stop.

    Returns
    -------
    tuple[RecorderState, Outputs]
        The disabled state and final events, records, spans and close command.

    Examples
    --------
    Stopping an already disabled recorder returns empty output.
    """
    if isinstance(state, Disabled):
        return state, Outputs()
    emission = _emit_unattributed(
        state.progress.window.entries,
        "supervisor_stopped",
        event.now,
        event.observed_at,
    )
    progress = dc.replace(state.progress, window=OutcomeWindow(), active=())
    commands = (
        (CloseQuery(state.ticket),)
        if isinstance(state, Querying) and state.ticket is not None
        else ()
    )
    spans = (
        (QueryFinished(event.now, "supervisor_stopped"),)
        if isinstance(state, Querying)
        else ()
    )
    output = _join_outputs(
        emission,
        Outputs(
            commands=commands, spans=spans, reading_reasons=("supervisor_stopped",)
        ),
    )
    return Disabled(progress), output
