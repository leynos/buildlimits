"""Build immutable outcome effects from completed recorder windows."""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_attribution import WindowEntry
from buildlimits.outcome_record_format import OutcomeRecord, WindowSummary
from buildlimits.outcome_recorder_types import (
    CloseQuery,
    Disabled,
    Idle,
    OutcomeEvent,
    Outputs,
    QueryFinished,
    Querying,
    RecorderState,
)

if typ.TYPE_CHECKING:
    import datetime as dt

    from buildlimits.outcome_attribution import Attribution
    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.outcome_recorder_types import (
        AttributionKind,
        PollResult,
        Progress,
        SpanTransition,
        _Launch,
    )
    from buildlimits.outcome_stats import StatsReading
    from buildlimits.outcome_vocabulary import CacheResult, OutcomeReason
    from buildlimits.outcome_window import OutcomeWindow


@dc.dataclass(frozen=True, slots=True)
class _EventDetails:
    """Common classification values applied to each event in one window."""

    cache_result: CacheResult
    attribution: AttributionKind
    reason: OutcomeReason
    group_count: int
    now: float


def _emit_unattributed(
    launches: tuple[_Launch, ...],
    reason: OutcomeReason,
    now: float,
    observed_at: dt.datetime | None,
) -> Outputs:
    """Build one refusal event for each buffered launch without splitting.

    Returns
    -------
    Outputs
        Events and keyed records carrying the same refusal reason.

    Examples
    --------
    An empty launch tuple returns ``Outputs()``.
    """
    details = _EventDetails("unattributed", "none", reason, len(launches), now)
    events = tuple(_event_for(launch, details) for launch in launches)
    records = tuple(
        record
        for launch in launches
        if (
            record := _record_for(
                launch,
                details,
                observed_at or launch.observed_at,
            )
        )
        is not None
    )
    return Outputs(events=events, records=records)


def _emit_attribution(
    launches: tuple[_Launch, ...],
    attribution: Attribution,
    now: float,
    readings: tuple[StatsReading, StatsReading],
) -> Outputs:
    """Build labels, records and a summary for one valid statistics window.

    Returns
    -------
    Outputs
        Per-launch effects and the reading reason for the completed window.

    Examples
    --------
    An accepted attribution is copied to each emitted event.
    """
    labels = dict(attribution.labels)

    def details_for(launch: _Launch) -> _EventDetails:
        """Build one event's classification from the shared window result.

        Returns
        -------
        _EventDetails
            The label or an explicit unattributed fallback for this launch.

        Examples
        --------
        A launch omitted from attribution receives ``"unattributed"``.
        """
        result = labels.get(launch.group_id, "unattributed")
        return _EventDetails(
            result,
            attribution.kind,
            attribution.reason,
            len(launches),
            now,
        )

    events = tuple(_event_for(launch, details_for(launch)) for launch in launches)
    observed_at = next(
        (launch.observed_at for launch in reversed(launches) if launch.observed_at),
        None,
    )
    records = tuple(
        record
        for launch in launches
        if (
            record := _record_for(
                launch,
                details_for(launch),
                observed_at or launch.observed_at,
            )
        )
        is not None
    )
    summary = (
        _window_summary(launches, readings, observed_at)
        if attribution.reason in {"none", "mixed_window"}
        else None
    )
    saved_records = (*records, summary) if summary is not None else records
    return Outputs(
        records=saved_records,
        events=events,
        reading_reasons=(attribution.reason,),
    )


def _event_for(launch: _Launch, details: _EventDetails) -> OutcomeEvent:
    """Convert one retained launch to the closed journal event shape.

    Returns
    -------
    OutcomeEvent
        One privacy-safe event with its runtime and window classification.

    Examples
    --------
    An event from a three-launch window records ``window_groups`` as ``3``.
    """
    runtime = (
        launch.runtime_seconds
        if launch.runtime_seconds is not None
        else max(0.0, details.now - launch.started_at)
    )
    return OutcomeEvent(
        cache_result=details.cache_result,
        outcome_attribution=details.attribution,
        crate_origin=launch.payload.origin,
        outcome_reason=details.reason,
        runtime_seconds=runtime,
        window_groups=details.group_count,
    )


def _record_for(
    launch: _Launch,
    details: _EventDetails,
    observed_at: dt.datetime | None,
) -> OutcomeRecord | None:
    """Build a keyed store record when this launch has a key and timestamp.

    Returns
    -------
    OutcomeRecord or None
        A keyed record, or ``None`` when the launch cannot be safely stored.

    Examples
    --------
    A launch without a cache key produces no record.
    """
    if launch.payload.key is None or observed_at is None:
        return None
    runtime = (
        launch.runtime_seconds
        if launch.runtime_seconds is not None
        else max(0.0, details.now - launch.started_at)
    )
    return OutcomeRecord(
        key=launch.payload.key,
        outcome=details.cache_result,
        origin=launch.payload.origin,
        runtime_seconds=runtime,
        observed_at=observed_at,
        parser_class=launch.payload.parser_class,
        child_sighting="unavailable",
        child_samples=0,
    )


def _window_summary(
    launches: tuple[_Launch, ...],
    readings: tuple[StatsReading, StatsReading],
    observed_at: dt.datetime | None,
) -> WindowSummary | None:
    """Summarize one valid window and its parser predictions.

    Returns
    -------
    WindowSummary or None
        Counter deltas and parser totals when the window has a timestamp.

    Examples
    --------
    A window without ``observed_at`` produces no persisted summary.
    """
    if observed_at is None:
        return None
    before, after = readings
    return WindowSummary(
        observed_at=observed_at,
        server_version=after.version,
        window_groups=len(launches),
        requests_not_compile=max(
            0, after.requests_not_compile - before.requests_not_compile
        ),
        requests_not_cacheable=max(
            0, after.requests_not_cacheable - before.requests_not_cacheable
        ),
        predicted_not_compile=sum(
            item.payload.parser_class == "not_compile" for item in launches
        ),
        predicted_declined=sum(
            item.payload.parser_class == "declined" for item in launches
        ),
        predicted_cacheable=sum(
            item.payload.parser_class == "cacheable" for item in launches
        ),
    )


def _attribution_window(window: OutcomeWindow[_Launch]) -> tuple[WindowEntry, ...]:
    """Project launch state into the attribution module's narrow input.

    Returns
    -------
    tuple[WindowEntry, ...]
        Group identifiers paired with their cleanliness observations.

    Examples
    --------
    An empty recorder window projects to ``()``.
    """
    return tuple(
        WindowEntry(entry.group_id, entry.is_clean) for entry in window.entries
    )


def _with_progress(
    state: RecorderState, progress: Progress, *, straddled: bool = False
) -> RecorderState:
    """Replace shared progress without changing the recorder typestate.

    Returns
    -------
    RecorderState
        The same enabled or disabled variant with the supplied progress.

    Examples
    --------
    Replacing progress on ``Idle()`` returns another idle state.
    """
    match state:
        case Querying():
            return dc.replace(
                state,
                progress=progress,
                has_straddled_launch=state.has_straddled_launch or straddled,
            )
        case Disabled():
            return dc.replace(state, progress=progress)
        case Idle():
            return Idle(progress)


def _emission(
    events: tuple[OutcomeEvent, ...],
    records: tuple[StoreRecord, ...],
    spans: tuple[SpanTransition, ...],
    reason: OutcomeReason,
) -> Outputs:
    """Create a terminal reading output with its reason in one place.

    Returns
    -------
    Outputs
        The supplied effects with exactly one reading reason.

    Examples
    --------
    An empty emission for a baseline read carries reason ``"none"``.
    """
    return Outputs(
        events=events, records=records, spans=spans, reading_reasons=(reason,)
    )


def _join_outputs(first: Outputs, second: Outputs) -> Outputs:
    """Combine effects from one reducer transition and its port response.

    Returns
    -------
    Outputs
        A new value containing each effect in original order.

    Examples
    --------
    Joining two empty outputs returns ``Outputs()``.
    """
    return Outputs(
        commands=(*first.commands, *second.commands),
        events=(*first.events, *second.events),
        records=(*first.records, *second.records),
        spans=(*first.spans, *second.spans),
        reading_reasons=(*first.reading_reasons, *second.reading_reasons),
    )


def _with_reading_reason(outputs: Outputs, reason: OutcomeReason) -> Outputs:
    """Attach a reading result when its event and record builder omitted it.

    Returns
    -------
    Outputs
        The existing output, or a copy with the supplied reason.

    Examples
    --------
    Adding ``"timeout"`` to an empty output sets its reading reason.
    """
    if outputs.reading_reasons:
        return outputs
    return dc.replace(outputs, reading_reasons=(reason,))


def _finish_state(
    state: Querying,
    progress: Progress,
    event: PollResult,
    emitted: Outputs,
) -> tuple[RecorderState, Outputs]:
    """Close a terminal ticket and join its span and classification effects.

    Returns
    -------
    tuple[RecorderState, Outputs]
        The idle state and terminal outputs, including a closed ticket.

    Raises
    ------
    RuntimeError
        If the terminal reading did not produce a reason.

    Examples
    --------
    Finishing a ticket with reason ``"none"`` closes it with that span reason.
    """
    if not emitted.reading_reasons:
        msg = "a terminal reading must carry its reason"
        raise RuntimeError(msg)
    reason = emitted.reading_reasons[-1]
    commands = () if state.ticket is None else (CloseQuery(state.ticket),)
    output = _join_outputs(
        emitted,
        Outputs(
            commands=commands,
            spans=(QueryFinished(event.now, reason),),
        ),
    )
    return Idle(progress), output
