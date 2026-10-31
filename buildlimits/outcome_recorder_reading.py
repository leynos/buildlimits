"""Finish statistics readings and schedule the next bounded query."""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_attribution import (
    Generation,
    Reading,
    attribute,
)
from buildlimits.outcome_ports import GenerationFailure
from buildlimits.outcome_recorder_outputs import (
    _attribution_window,
    _emission,
    _emit_attribution,
    _emit_unattributed,
    _finish_state,
    _with_reading_reason,
)
from buildlimits.outcome_recorder_types import (
    CloseQuery,
    Disabled,
    Idle,
    Outputs,
    QueryFinished,
    Querying,
)
from buildlimits.outcome_window import OutcomeWindow

if typ.TYPE_CHECKING:
    from buildlimits.outcome_recorder_types import PollResult, Progress, RecorderState
    from buildlimits.outcome_stats import StatsReading
    from buildlimits.outcome_vocabulary import OutcomeReason

OUTCOME_READ_SECONDS = 1.0
OUTCOME_MAX_BACKOFF_SECONDS = 60.0
_RETRYABLE_READING_REASONS = frozenset({
    "timeout",
    "read_error",
    "parse_error",
    "unsupported_version",
    "generation_unavailable",
    "empty_stats",
    "counter_decreased",
    "count_mismatch",
})


def _finish_failure(
    state: Querying,
    now: float,
    reason: OutcomeReason,
    *,
    disable: bool = False,
) -> tuple[RecorderState, Outputs]:
    """Refuse an unusable reading and apply reason-specific back-off.

    Returns
    -------
    tuple[RecorderState, Outputs]
        The next state and one refusal output, with the ticket closed if present.

    Examples
    --------
    ``_finish_failure(querying, 4.0, "timeout")[1].reading_reasons`` is
    ``("timeout",)``.
    """
    emitted = _emit_unattributed(
        state.progress.window.entries,
        reason,
        now,
        None,
    )
    progress = _schedule_next_read(
        _clear_window(state.progress),
        now,
        reason,
    )
    progress = dc.replace(progress, baseline=None)
    commands = () if state.ticket is None else (CloseQuery(state.ticket),)
    spans = (QueryFinished(now, reason),)
    output = Outputs(
        commands=commands,
        events=emitted.events,
        records=emitted.records,
        spans=spans,
        reading_reasons=(reason,),
    )
    next_state: RecorderState = Disabled(progress) if disable else Idle(progress)
    return next_state, output


def _finish_reading(
    state: Querying, event: PollResult, stats: StatsReading
) -> tuple[RecorderState, Outputs]:
    """Validate generations before attributing a complete quiet window.

    Returns
    -------
    tuple[RecorderState, Outputs]
        The idle state with its accepted baseline and the completed effects.

    Examples
    --------
    A valid zero reading with no launches returns the reason ``"none"``.
    """
    after_value = event.after_generation
    after_generation = after_value if isinstance(after_value, Generation) else None
    baseline = state.progress.baseline
    reason = _generation_reason(state, baseline, after_value)
    if reason is not None:
        candidate = (
            Reading(after_generation, stats)
            if after_generation is not None and not state.has_straddled_launch
            else None
        )
        return _refuse_generation(state, event, candidate, reason)
    after = Reading(after_generation, stats)
    if state.has_straddled_launch:
        return _finish_straddled(state, event)
    if baseline is None:
        return _establish_baseline(state, event, after)
    return _attribute_window(state, event, baseline, after)


def _refuse_generation(
    state: Querying,
    event: PollResult,
    candidate: Reading | None,
    reason: OutcomeReason,
) -> tuple[RecorderState, Outputs]:
    """Refuse an unusable generation boundary and reschedule the next read.

    Returns
    -------
    tuple[RecorderState, Outputs]
        The idle state with any safe candidate baseline and its refusal output.
    """
    emitted = _emit_unattributed(
        state.progress.window.entries,
        reason,
        event.now,
        None,
    )
    progress = _schedule_next_read(
        _clear_window(state.progress),
        event.now,
        reason,
    )
    progress = dc.replace(progress, baseline=candidate)
    emitted = _with_reading_reason(emitted, reason)
    return _finish_state(state, progress, event, emitted)


def _finish_straddled(
    state: Querying, event: PollResult
) -> tuple[RecorderState, Outputs]:
    """Reschedule once a launch straddled the two readings' boundaries.

    Returns
    -------
    tuple[RecorderState, Outputs]
        The idle state retimed for a later read and its empty output.
    """
    progress = _schedule_next_read(
        state.progress,
        event.now,
        "straddled_reading",
    )
    empty = _emission((), (), (), "straddled_reading")
    return _finish_state(state, progress, event, empty)


def _establish_baseline(
    state: Querying, event: PollResult, after: Reading
) -> tuple[RecorderState, Outputs]:
    """Accept the first usable reading as baseline, refusing its window.

    Returns
    -------
    tuple[RecorderState, Outputs]
        The idle state holding the new baseline and its reading output.
    """
    emitted, reason = _baseline_emission(state, event)
    progress = _schedule_next_read(
        _clear_window(state.progress),
        event.now,
        reason,
    )
    progress = dc.replace(progress, baseline=after)
    emitted = _with_reading_reason(emitted, reason)
    return _finish_state(state, progress, event, emitted)


def _baseline_emission(
    state: Querying, event: PollResult
) -> tuple[Outputs, OutcomeReason]:
    """Describe an unaccountable window as a refusal or a quiet no-op.

    Returns
    -------
    tuple[Outputs, OutcomeReason]
        The emitted effects and the reason recorded for the reading.
    """
    if not state.progress.window.entries:
        return _emission((), (), (), "none"), "none"
    emitted = _emit_unattributed(
        state.progress.window.entries,
        "no_baseline",
        event.now,
        None,
    )
    return emitted, "no_baseline"


def _attribute_window(
    state: Querying,
    event: PollResult,
    baseline: Reading,
    after: Reading,
) -> tuple[RecorderState, Outputs]:
    """Attribute the window between two accepted readings.

    Returns
    -------
    tuple[RecorderState, Outputs]
        The idle state advancing to ``after`` and the attributed output.
    """
    attribution = attribute(baseline, after, _attribution_window(state.progress.window))
    emitted = _emit_attribution(
        state.progress.window.entries,
        attribution,
        event.now,
        (baseline.stats, after.stats),
    )
    progress = _schedule_next_read(
        dc.replace(_clear_window(state.progress), baseline=after),
        event.now,
        attribution.reason,
    )
    return _finish_state(state, progress, event, emitted)


def _generation_reason(
    state: Querying,
    baseline: Reading | None,
    after: Generation | GenerationFailure | None,
) -> OutcomeReason | None:
    """Return a generation refusal before comparing sccache counters.

    Returns
    -------
    OutcomeReason or None
        The generation failure reason, or ``None`` when both boundaries agree.

    Examples
    --------
    A failed identity read is unavailable; a change to or from no server is
    ``"generation_changed"``.
    """
    before = state.before_generation
    if _is_unavailable(before, after):
        return "generation_unavailable"
    if before is None or after is None:
        return "generation_changed"
    if baseline is not None and baseline.generation != before:
        return "generation_changed"
    return None if before == after else "generation_changed"


def _is_unavailable(
    before: Generation | GenerationFailure | None,
    after: Generation | GenerationFailure | None,
) -> bool:
    """Report whether either boundary failed or both are absent.

    Returns
    -------
    bool
        True when no comparable pair of server identities exists.
    """
    if isinstance(before, GenerationFailure) or isinstance(after, GenerationFailure):
        return True
    return before is None and after is None


def _clear_window(progress: Progress) -> Progress:
    """Drop accounted launches while retaining only open active work.

    Returns
    -------
    Progress
        The same baseline and active launches with an empty outcome window.

    Examples
    --------
    Clearing a progress value leaves ``progress.window.entries`` empty.
    """
    accounted = {entry.group_id for entry in progress.window.entries}
    return dc.replace(
        progress,
        window=OutcomeWindow(),
        active=tuple(
            item for item in progress.active if item.group_id not in accounted
        ),
    )


def _schedule_next_read(
    progress: Progress, now: float, reason: OutcomeReason
) -> Progress:
    """Apply a one-second interval or bounded same-cause exponential back-off.

    Returns
    -------
    Progress
        Updated timing and failure state for the next scheduler-approved read.

    Examples
    --------
    A first timeout at ``4.0`` sets ``next_read_at`` to ``5.0``.
    """
    if reason not in _RETRYABLE_READING_REASONS:
        return dc.replace(
            progress,
            next_read_at=now + OUTCOME_READ_SECONDS,
            failure_count=0,
            last_failure=None,
        )
    failure_count = progress.failure_count + 1 if progress.last_failure == reason else 1
    max_exponent = int(OUTCOME_MAX_BACKOFF_SECONDS).bit_length()
    wait = min(
        OUTCOME_MAX_BACKOFF_SECONDS,
        2.0 ** min(failure_count - 1, max_exponent),
    )
    return dc.replace(
        progress,
        next_read_at=now + wait,
        failure_count=failure_count,
        last_failure=reason,
    )
