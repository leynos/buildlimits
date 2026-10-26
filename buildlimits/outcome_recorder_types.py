"""Immutable events, commands and typestates for outcome recording."""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_window import OutcomeWindow

if typ.TYPE_CHECKING:
    import datetime as dt

    from buildlimits.outcome_attribution import Generation, Reading
    from buildlimits.outcome_gate import QuietInputs
    from buildlimits.outcome_key import CacheKey
    from buildlimits.outcome_parser import ParserClass
    from buildlimits.outcome_ports import (
        GenerationFailure,
        Pending,
        QueryFailure,
        StatisticsText,
        Ticket,
    )
    from buildlimits.outcome_record_format import ChildSighting, Origin, StoreRecord
    from buildlimits.outcome_vocabulary import CacheResult, OutcomeReason

type AttributionKind = typ.Literal["solo", "unanimous", "inferred", "none"]


@dc.dataclass(frozen=True, slots=True)
class LaunchPayload:
    """Privacy-safe metadata retained for one cached launch."""

    key: CacheKey | None
    origin: Origin
    parser_class: ParserClass


@dc.dataclass(frozen=True, slots=True)
class OutcomeEvent:
    """One journal-ready classification for a cached launch."""

    cache_result: CacheResult
    outcome_attribution: AttributionKind
    crate_origin: Origin
    outcome_reason: OutcomeReason
    runtime_seconds: float
    window_groups: int
    child_sighting: ChildSighting = "unavailable"
    child_samples: int = 0


@dc.dataclass(frozen=True, slots=True)
class Launched:
    """A process group began, with key metadata only for cached launches."""

    group_id: str
    is_cached: bool
    payload: LaunchPayload
    now: float


@dc.dataclass(frozen=True, slots=True)
class LaunchFailed:
    """A group failed before it could enter the cache outcome window."""

    group_id: str


@dc.dataclass(frozen=True, slots=True)
class Ended:
    """A group ended and its cache-window cleanliness became known."""

    group_id: str
    is_clean: bool
    now: float
    observed_at: dt.datetime


@dc.dataclass(frozen=True, slots=True)
class GateOpen:
    """A scheduler-approved read request with its current quiet-point inputs."""

    inputs: QuietInputs
    now: float


@dc.dataclass(frozen=True, slots=True)
class PollResult:
    """One start or poll result from the statistics and generation ports."""

    now: float
    result: Ticket | Pending | StatisticsText | QueryFailure
    before_generation: Generation | GenerationFailure | None = None
    after_generation: Generation | GenerationFailure | None = None


@dc.dataclass(frozen=True, slots=True)
class Stopping:
    """A graceful stop must close a ticket and account for every launch."""

    now: float
    observed_at: dt.datetime


type RecorderEvent = Launched | LaunchFailed | Ended | GateOpen | PollResult | Stopping


@dc.dataclass(frozen=True, slots=True)
class StartQuery:
    """Command for the shell to start one bounded statistics read."""

    now: float


@dc.dataclass(frozen=True, slots=True)
class CloseQuery:
    """Command for the shell to close a terminal statistics ticket."""

    ticket: Ticket


@dc.dataclass(frozen=True, slots=True)
class QueryStarted:
    """Span transition emitted when a statistics query is requested."""

    now: float


@dc.dataclass(frozen=True, slots=True)
class QueryFinished:
    """Span transition emitted when a statistics query reaches a terminal result."""

    now: float
    reason: OutcomeReason


type RecorderCommand = StartQuery | CloseQuery
type SpanTransition = QueryStarted | QueryFinished


@dc.dataclass(frozen=True, slots=True)
class Outputs:
    """Effects returned by the reducer for its caller to publish or execute."""

    commands: tuple[RecorderCommand, ...] = ()
    events: tuple[OutcomeEvent, ...] = ()
    records: tuple[StoreRecord, ...] = ()
    spans: tuple[SpanTransition, ...] = ()
    reading_reasons: tuple[OutcomeReason, ...] = ()


@dc.dataclass(frozen=True, slots=True)
class _Launch:
    """One launch retained until a window closes or a refusal is emitted."""

    group_id: str
    payload: LaunchPayload
    started_at: float
    runtime_seconds: float | None = None
    is_clean: bool = False
    observed_at: dt.datetime | None = None


@dc.dataclass(frozen=True, slots=True)
class Progress:
    """Shared counters, baseline, window and active launches across typestates."""

    baseline: Reading | None = None
    window: OutcomeWindow[_Launch] = dc.field(default_factory=OutcomeWindow)
    active: tuple[_Launch, ...] = ()
    next_read_at: float = 0.0
    failure_count: int = 0
    last_failure: OutcomeReason | None = None


@dc.dataclass(frozen=True, slots=True)
class Idle:
    """Recorder is enabled and no statistics query is in flight."""

    progress: Progress = Progress()


@dc.dataclass(frozen=True, slots=True)
class Querying:
    """Recorder is waiting for one statistics query result."""

    progress: Progress
    started_at: float
    ticket: Ticket | None = None
    before_generation: Generation | GenerationFailure | None = None
    has_straddled_launch: bool = False


@dc.dataclass(frozen=True, slots=True)
class Disabled:
    """Recorder is terminal until the supervisor restarts."""

    progress: Progress


type RecorderState = Idle | Querying | Disabled
