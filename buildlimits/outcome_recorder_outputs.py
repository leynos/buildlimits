"""Build immutable outcome effects from completed recorder windows.

The effect combinations one transition needs — joining a reducer step to its
port response, attaching a reading reason, closing a terminal ticket — live in
:mod:`buildlimits.outcome_recorder_effects`, which also owns the two typestate
projections a transition needs. This module owns the mapping from a retained
launch to a journal event, a store row and a window summary.
"""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_record_format import (
    OutcomeRecord,
    StoreRecord,
    WindowSummary,
)
from buildlimits.outcome_recorder_types import (
    OutcomeEvent,
    Outputs,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_attribution import Attribution
    from buildlimits.outcome_record_format import LabelTimeProvenance
    from buildlimits.outcome_recorder_types import (
        AttributionKind,
        _Launch,
    )
    from buildlimits.outcome_stats import StatsReading
    from buildlimits.outcome_vocabulary import (
        CacheResult,
        OutcomeReason,
        RecordOmission,
    )


@dc.dataclass(frozen=True, slots=True)
class _EmissionMoment:
    """The monotonic tick and evidence one emission is caused at.

    ``observed_at`` is when the label or refusal became known. It is ``None``
    only for a purely synthetic event, which supplies no wall clock.

    ``generation_digest`` names the sccache generation the reading observed at
    its window's end. It is ``None`` whenever no single generation can be
    attributed to the rows: a refused, overflowing or changed window, and every
    event emitted without a reading at all. It is never inferred from a
    neighbouring window, so a null digest is a measured absence rather than a
    missing value.
    """

    now: float
    observed_at: dt.datetime | None = None
    generation_digest: str | None = None


@dc.dataclass(frozen=True, slots=True)
class _TimingEvidence:
    """The complete version 3 timing group one row may carry.

    A typed value rather than a mapping keeps each field's own type, so a
    completion time cannot be read as an attempt identifier. The moment itself
    is the reading's own, supplied by the emission; this value adds only the
    provenance that says which invocation the row belongs to.
    """

    completed_at: dt.datetime | None
    attempt: str
    supervisor_generation: str


@dc.dataclass(frozen=True, slots=True)
class _EventDetails:
    """Common classification values applied to each event in one window."""

    cache_result: CacheResult
    attribution: AttributionKind
    reason: OutcomeReason
    group_count: int
    now: float


@dc.dataclass(frozen=True, slots=True)
class _RowEvidence:
    """The evidence one emission's store rows carry, decided once per window.

    ``observed_at`` is the moment every row shares, ``generation_digest`` names
    the sccache generation that produced them, and ``provenance`` states
    whether that moment is proven to be when the label became known. They
    travel together because they answer one question — what does this row
    prove? — and separating them invites a time without its proof.
    """

    observed_at: dt.datetime
    generation_digest: str | None
    provenance: LabelTimeProvenance | None


def _emit_unattributed(
    launches: tuple[_Launch, ...],
    reason: OutcomeReason,
    moment: _EmissionMoment,
) -> Outputs:
    """Build one refusal event for each buffered launch without splitting.

    ``moment.observed_at`` is the moment the refusal became known, carried by
    the event that caused this emission: a completed reading, a stopped
    supervisor or an overflowing launch. Every row in the window shares it, so
    a label can never be released before the evidence that produced it existed.

    Returns
    -------
    Outputs
        Events and keyed records carrying the same refusal reason. Every event
        states whether its outcome became a row, so a launch that cannot be
        stored is counted by cause rather than lost between the two totals.

    Examples
    --------
    An empty launch tuple returns ``Outputs()``.
    """
    details = _EventDetails("unattributed", "none", reason, len(launches), moment.now)
    return _pair_events_and_records(
        launches, lambda _launch: details, moment.observed_at, moment.generation_digest
    )


def _pair_events_and_records(
    launches: tuple[_Launch, ...],
    details_for: cabc.Callable[[_Launch], _EventDetails],
    observed_at: dt.datetime | None,
    generation_digest: str | None = None,
) -> Outputs:
    """Build each launch's event and row from one shared decision.

    The omission reason is computed once per launch and copied onto the event,
    so the journal and the store cannot disagree about whether a *pre-store*
    omission applied to that outcome. That is all the pairing proves: the row
    is a candidate the writer may still discard or leave queued, so the shared
    reason is not evidence that the outcome reached the file.

    ``generation_digest`` is copied onto every row this emission builds, and is
    ``None`` for a window no single generation can be attributed to. The caller
    decides that, because only the reading knows whether its two boundaries
    named one server.

    Returns
    -------
    Outputs
        The events, each tagged with its omission reason, and the stored rows.
    """
    events: list[OutcomeEvent] = []
    records: list[StoreRecord] = []
    # A row is proven only when the emission's own moment dated it. The
    # completion fallback exists for a purely synthetic event that carries no
    # wall clock, and a completion time is a lower bound on when the label
    # became known, so that row must not claim provenance the shell never
    # supplied.
    provenance: LabelTimeProvenance | None = (
        "verified" if observed_at is not None else None
    )
    for launch in launches:
        details = details_for(launch)
        row_time = _row_time(launch, observed_at)
        omission = record_omission(launch, row_time)
        events.append(_event_for(launch, details, omission=omission))
        evidence = (
            None
            if omission != "none" or row_time is None
            else _RowEvidence(row_time, generation_digest, provenance)
        )
        record = None if evidence is None else _record_for(launch, details, evidence)
        if record is not None:
            records.append(record)
    return Outputs(events=tuple(events), records=tuple(records))


def _emit_attribution(
    launches: tuple[_Launch, ...],
    attribution: Attribution,
    moment: _EmissionMoment,
    readings: tuple[StatsReading, StatsReading],
) -> Outputs:
    """Build labels, records and a summary for one valid statistics window.

    ``moment.observed_at`` is the moment the reading that produced these
    labels completed, and every row and the summary share it. A per-launch
    completion time is never substituted: a label does not exist until its
    query returns, so releasing it at the launch's own stamp would let a
    prediction read a label that had not been observed yet.

    Returns
    -------
    Outputs
        Per-launch effects and the reading reason for the completed window.
        Every event states whether its outcome became a row, so a launch that
        cannot be stored is counted by cause.

    Examples
    --------
    An accepted attribution is copied to each emitted event.
    """
    labels = dict(attribution.labels)
    now = moment.now

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

    paired = _pair_events_and_records(
        launches, details_for, moment.observed_at, moment.generation_digest
    )
    summary = (
        _window_summary(launches, readings, moment.observed_at)
        if attribution.reason in {"none", "mixed_window"}
        else None
    )
    saved_records = (
        (*paired.records, summary) if summary is not None else paired.records
    )
    return Outputs(
        records=saved_records,
        events=paired.events,
        reading_reasons=(attribution.reason,),
    )


def _event_for(
    launch: _Launch, details: _EventDetails, *, omission: RecordOmission = "none"
) -> OutcomeEvent:
    """Convert one retained launch to the closed journal event shape.

    ``omission`` names why this event produced no store row. An event and its
    row are built from the same launch, so the reason recorded here is the same
    decision ``_record_for`` took. It is not a reconciliation of the journal
    against the store: an appended row can still be discarded by a failed write
    or be an outstanding queued write, and only the omission reasons are
    measured per outcome.

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
        child_sighting=launch.sightings.sighting(),
        child_samples=launch.sightings.samples,
        record_omitted=omission,
    )


def record_omission(launch: _Launch, observed_at: dt.datetime | None) -> RecordOmission:
    """Return why one launch's outcome cannot become a store row.

    Both clauses are measured, not inferred: a row needs a salted identity to
    correlate with its admission, and a wall-clock moment at which its label or
    refusal became known. The reason is a closed token so the observer can count
    the omitted outcomes by cause and reconcile them with the events it
    published.

    Returns
    -------
    RecordOmission
        ``none`` when the outcome is storable, else the reason it is not.
    """
    if launch.payload.key is None:
        return "key_unavailable"
    if observed_at is None:
        return "label_time_unavailable"
    return "none"


def _row_time(launch: _Launch, observed_at: dt.datetime | None) -> dt.datetime | None:
    """Return the wall-clock moment one row's label or refusal became known.

    Every row a reading emits shares the reading's own moment, never a
    launch's completion time: a label does not exist until its query returns.
    Only a purely synthetic event, which carries no wall clock at all, falls
    back to the launch's own completion time, so a unit test can still assert
    the row shape. The shell stamps every gate, launch, poll and stop, so a
    real row never takes that path.

    Returns
    -------
    datetime or None
        The window's moment, or the launch's completion when none was given.
    """
    if observed_at is not None:
        return observed_at
    return launch.completed_at


def _record_for(
    launch: _Launch,
    details: _EventDetails,
    evidence: _RowEvidence,
) -> OutcomeRecord | None:
    """Build a keyed store record when this launch has a key and evidence.

    ``evidence`` carries the time, provenance and generation this emission
    decided once for the whole window, so a row cannot claim a time without the
    proof that belongs to it. Its ``generation_digest`` is ``None`` when no
    single generation can be attributed to the row, which is a measured absence
    and never a guess.

    Returns
    -------
    OutcomeRecord or None
        A keyed record, or ``None`` when the launch cannot be safely stored.

    Examples
    --------
    A launch without a cache key produces no record.
    """
    if launch.payload.key is None:
        return None
    runtime = (
        launch.runtime_seconds
        if launch.runtime_seconds is not None
        else max(0.0, details.now - launch.started_at)
    )
    verified = _timing_evidence(launch)
    return OutcomeRecord(
        key=launch.payload.key,
        outcome=details.cache_result,
        origin=launch.payload.origin,
        runtime_seconds=runtime,
        observed_at=evidence.observed_at,
        parser_class=launch.payload.parser_class,
        child_sighting=launch.sightings.sighting(),
        child_samples=launch.sightings.samples,
        outcome_reason=details.reason,
        outcome_attribution=details.attribution,
        admitted_at=launch.admitted_at,
label_time_provenance=evidence.provenance,
        generation_digest=evidence.generation_digest,
        completed_at=None if verified is None else verified.completed_at,
        attempt=None if verified is None else verified.attempt,
        supervisor_generation=(
            None if verified is None else verified.supervisor_generation
        ),
    )


def _timing_evidence(launch: _Launch) -> _TimingEvidence | None:
    """Return the version 5 correlation group for one launch, or no group.

    The moment a row claims is the reading's own, which the recorder stamps
    from the event that produced it; the label-time provenance marker beside it
    records that the observer accepted the reading. What this group adds is
    identity: the attempt that produced the row and the supervisor generation
    whose clock dated it. A payload without a generation predates that
    evidence, so no group is written and the row states an explicit null,
    which the replay reads as uncorrelated rather than as proof.

    Returns
    -------
    _TimingEvidence | None
        The launch's completion moment and correlation tokens, or None.
    """
    generation = launch.payload.supervisor_generation
    if generation is None:
        return None
    return _TimingEvidence(
        completed_at=launch.completed_at,
        attempt=launch.group_id,
        supervisor_generation=generation,
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
