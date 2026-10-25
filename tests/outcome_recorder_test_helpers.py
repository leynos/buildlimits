"""Shared fixtures for the cache-outcome recorder contracts."""

import dataclasses as dc
import typing as typ

from outcome_test_helpers import (
    GENERATION,
    OBSERVED_AT,
    _TimedGateEvent,
    cache_key,
    clear_gate_event,
    launch_payload,
    outcome_module,
    stats_reading,
    stats_text,
)

__all__ = (
    "GENERATION",
    "OBSERVED_AT",
    "WindowSetup",
    "baseline_state",
    "begin_fresh_recorder_query",
    "begin_query",
    "begin_single_cached_query",
    "cache_key",
    "clear_gate_event",
    "complete_query",
    "launch_payload",
    "outcome_module",
    "recorder_progress",
    "recorder_ticket",
    "state_with_window",
    "stats_reading",
    "stats_text",
)

if typ.TYPE_CHECKING:
    from types import ModuleType


class _OutcomeWindow(typ.Protocol):
    """The bounded fields that recorder tests inspect on an outcome window."""

    entries: tuple[object, ...]
    is_overflowed: bool


class _Reading(typ.Protocol):
    """The generation field inspected on a recorder baseline."""

    generation: object


class _Progress(typ.Protocol):
    """The reducer progress fields inspected by the recorder contracts."""

    baseline: _Reading | None
    window: _OutcomeWindow
    active: tuple[object, ...]
    next_read_at: float
    failure_count: int
    last_failure: str | None


class _RecorderState(typ.Protocol):
    """The typestate fields read by the recorder contracts."""

    progress: _Progress
    ticket: object | None


class _OutcomeEvent(typ.Protocol):
    """The label fields checked on recorder outcome events."""

    cache_result: str
    outcome_attribution: str
    outcome_reason: str


class _OutcomeRecord(typ.Protocol):
    """The stable keyed value checked on persisted outcome records."""

    key: object


class _Outputs(typ.Protocol):
    """The pure reducer output fields inspected by recorder tests."""

    commands: tuple[object, ...]
    events: tuple[_OutcomeEvent, ...]
    records: tuple[_OutcomeRecord, ...]
    reading_reasons: tuple[str, ...]


def _recorder_state(state: object) -> _RecorderState:
    """Narrow a lazy reducer value to its public typestate contract.

    Returns
    -------
    _RecorderState
        A view of the progress and ticket fields used by these tests.

    Examples
    --------
    ``_recorder_state(querying).ticket`` reads its opaque ticket.
    """
    return typ.cast("_RecorderState", state)


def recorder_progress(state: object) -> _Progress:
    """Read progress from a lazily imported reducer state.

    Returns
    -------
    _Progress
        The current baseline and bounded outcome window.

    Examples
    --------
    ``recorder_progress(state).baseline`` reads the accepted baseline.
    """
    return _recorder_state(state).progress


def recorder_ticket(state: object) -> object | None:
    """Read the opaque query ticket from a lazily imported reducer state.

    Returns
    -------
    object or None
        The ticket held by a querying state, if one has been started.

    Examples
    --------
    ``recorder_ticket(querying)`` is the value passed to ``CloseQuery``.
    """
    return _recorder_state(state).ticket


def baseline_state(recorder: ModuleType, baseline: object | None = None) -> object:
    """Build an idle recorder state with one established baseline.

    Returns
    -------
    object
        An idle state paired with the supplied reading or a zero baseline.

    Examples
    --------
    ``baseline_state(outcome_module("outcome_recorder"))`` starts with a
    zero reading in the fixed generation.
    """
    attribution = outcome_module("outcome_attribution")
    selected = stats_reading() if baseline is None else baseline
    reading = attribution.Reading(GENERATION, selected)
    return recorder.Idle(progress=recorder.Progress(baseline=reading))


@dc.dataclass(frozen=True, slots=True)
class WindowSetup:
    """Parameters for a recorder state with completed cached launches."""

    payload: object
    count: int = 1
    clean: bool = True
    baseline: object | None = None


def state_with_window(recorder: ModuleType, setup: WindowSetup) -> object:
    """Build a baseline and a bounded window of completed cached launches.

    Returns
    -------
    object
        An idle state with ``setup.count`` cached launches in its window.

    Examples
    --------
    A default window contains one completed launch:
    ``len(recorder_progress(state_with_window(recorder, setup)).window.entries)``
    is ``1`` when ``setup.count`` is one.
    """
    state = baseline_state(recorder, setup.baseline)
    for index in range(setup.count):
        group_id = f"case-{index}"
        state, _ = recorder.advance(
            state,
            recorder.Launched(
                group_id=group_id,
                is_cached=True,
                payload=setup.payload,
                now=float(index),
            ),
        )
        state, _ = recorder.advance(
            state,
            recorder.Ended(
                group_id=group_id,
                is_clean=setup.clean,
                now=float(index) + 0.25,
                observed_at=OBSERVED_AT,
            ),
        )
    return state


def begin_query(
    state: object,
    recorder: ModuleType,
    gate_event: _TimedGateEvent,
    *,
    before_generation: object = GENERATION,
) -> tuple[object, _Outputs]:
    """Advance through a clear gate and the fake query-start reply.

    Returns
    -------
    tuple[object, _Outputs]
        The querying state and the output that contains ``StartQuery``.

    Examples
    --------
    ``begin_query(recorder.Idle(), recorder, gate_event)[1]`` contains the
    start command. Use ``before_generation=None`` to model a refused connection.
    """
    ports = outcome_module("outcome_ports")
    query_at = gate_event.now
    state, started = recorder.advance(state, gate_event)
    state, _ = recorder.advance(
        state,
        recorder.PollResult(
            now=query_at,
            result=ports.Ticket(serial=1, deadline=query_at + 2.0),
            before_generation=before_generation,
        ),
    )
    return state, started


def begin_single_cached_query(
    recorder: ModuleType, key: object, now: float
) -> tuple[object, _Outputs]:
    """Build one completed cached launch and start its statistics query.

    Returns
    -------
    tuple[object, _Outputs]
        The querying state and output containing the query-start command.

    Examples
    --------
    ``begin_single_cached_query(recorder, key, 1.0)[0]`` is querying.
    """
    payload = launch_payload(recorder, key)
    state = state_with_window(recorder, WindowSetup(payload))
    return begin_query(state, recorder, clear_gate_event(recorder, now))


def begin_fresh_recorder_query() -> tuple[ModuleType, object, _Outputs]:
    """Start a query on a fresh recorder for baseline or stop contracts.

    Returns
    -------
    tuple[ModuleType, object, _Outputs]
        The recorder, querying state and output containing the start command.

    Examples
    --------
    ``begin_fresh_recorder_query()[1]`` is the querying state.
    """
    recorder = outcome_module("outcome_recorder")
    state, output = begin_query(
        recorder.Idle(), recorder, clear_gate_event(recorder, 0.0)
    )
    return recorder, state, output


def complete_query(
    state: object, recorder: ModuleType, text: str, now: float
) -> tuple[object, _Outputs]:
    """Submit one completed statistics query from the same generation.

    Returns
    -------
    tuple[object, _Outputs]
        The updated state and outputs from the completed reading.

    Examples
    --------
    ``complete_query(querying, recorder, stats_text(), 1.0)`` returns the
    accepted baseline and its reading output.
    """
    ports = outcome_module("outcome_ports")
    return recorder.advance(
        state,
        recorder.PollResult(
            now=now,
            result=ports.StatisticsText(text),
            after_generation=GENERATION,
        ),
    )
