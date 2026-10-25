"""Closed reading-reason outcomes from the pure recorder state machine."""

import typing as typ

import pytest
from outcome_recorder_test_helpers import (
    GENERATION,
    WindowSetup,
    begin_fresh_recorder_query,
    begin_query,
    begin_single_cached_query,
    cache_key,
    clear_gate_event,
    launch_payload,
    outcome_module,
    recorder_progress,
    recorder_ticket,
    state_with_window,
    stats_reading,
    stats_text,
)

if typ.TYPE_CHECKING:
    from types import ModuleType


def _reading_result(failure: str, recorder_ports: ModuleType) -> tuple[object, object]:
    """Return the fake-port reading that reaches one named D13 reason."""
    changed_generation = outcome_module("outcome_attribution").Generation(
        "run-b", 11, 21
    )
    generation_failure = recorder_ports.GenerationFailure("generation_unavailable")
    result_by_failure = {
        "timeout": (recorder_ports.QueryFailure("timeout"), GENERATION),
        "read_error": (recorder_ports.QueryFailure("read_error"), GENERATION),
        "parse_error": (recorder_ports.StatisticsText("not JSON"), GENERATION),
        "unsupported_version": (
            recorder_ports.StatisticsText('{"version":"9.9.9","stats":{}}'),
            GENERATION,
        ),
        "generation_unavailable": (
            recorder_ports.StatisticsText(stats_text(compile_requests=1)),
            generation_failure,
        ),
        "generation_changed": (
            recorder_ports.StatisticsText(stats_text(compile_requests=1)),
            changed_generation,
        ),
        "empty_stats": (recorder_ports.StatisticsText(stats_text()), GENERATION),
        "counter_decreased": (
            recorder_ports.StatisticsText(
                stats_text(compile_requests=1, requests_executed=1, cache_hits=1)
            ),
            GENERATION,
        ),
        "ineligible_window": (
            recorder_ports.StatisticsText(
                stats_text(compile_requests=1, requests_executed=1, cache_hits=1)
            ),
            GENERATION,
        ),
        "count_mismatch": (
            recorder_ports.StatisticsText(stats_text(compile_requests=1, cache_hits=1)),
            GENERATION,
        ),
        "mixed_window": (
            recorder_ports.StatisticsText(
                stats_text(
                    compile_requests=2,
                    requests_executed=2,
                    cache_hits=1,
                    cache_misses=1,
                    compilations=1,
                )
            ),
            GENERATION,
        ),
    }
    return result_by_failure[failure]


_RETRY_POLL_DELAY_SECONDS = 0.1


def _poll_retry_failure(
    state: object,
    recorder: ModuleType,
    query_at: float,
    failure: typ.Literal["timeout", "read_error"],
) -> object:
    """Run one due retry and return its failed reading state.

    Example:
        state = _poll_retry_failure(state, recorder, 1.1, "timeout")
        assert recorder_progress(state).failure_count == 2
    """
    state, _ = begin_query(state, recorder, clear_gate_event(recorder, query_at))
    state, _ = recorder.advance(
        state,
        recorder.PollResult(
            now=query_at + _RETRY_POLL_DELAY_SECONDS,
            result=outcome_module("outcome_ports").QueryFailure(failure),
        ),
    )
    return state


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        ("timeout", "timeout"),
        ("read_error", "read_error"),
        ("parse_error", "parse_error"),
        ("unsupported_version", "unsupported_version"),
        ("generation_unavailable", "generation_unavailable"),
        ("generation_changed", "generation_changed"),
        ("empty_stats", "empty_stats"),
        ("counter_decreased", "counter_decreased"),
        ("ineligible_window", "ineligible_window"),
        ("count_mismatch", "count_mismatch"),
        ("mixed_window", "mixed_window"),
    ],
)
def test_each_reading_failure_is_named_and_unattributed(
    failure: str, expected_reason: str
) -> None:
    """Map every D13 failure to its reason without guessing a label."""
    recorder = outcome_module("outcome_recorder")
    ports = outcome_module("outcome_ports")
    count = 2 if failure == "mixed_window" else 1
    baseline = (
        stats_reading(compile_requests=2, requests_executed=2, cache_hits=1)
        if failure == "counter_decreased"
        else stats_reading()
    )
    payload = launch_payload(recorder, cache_key())
    state = state_with_window(
        recorder,
        WindowSetup(
            payload,
            count=count,
            clean=failure != "ineligible_window",
            baseline=baseline,
        ),
    )
    if failure in {"ineligible_window", "mixed_window"}:
        progress = recorder_progress(state)
        state = recorder.Idle(
            progress=recorder.Progress(
                baseline=progress.baseline,
                window=progress.window,
                active=progress.active,
                next_read_at=progress.next_read_at,
                failure_count=2,
                last_failure="timeout",
            )
        )
    state, _ = begin_query(state, recorder, clear_gate_event(recorder, 2.0))
    result, after_generation = _reading_result(failure, ports)
    state, output = recorder.advance(
        state,
        recorder.PollResult(
            now=2.1,
            result=result,
            after_generation=after_generation,
        ),
    )

    assert output.events, "a failed close supplies one row per launch"
    assert all(event.cache_result == "unattributed" for event in output.events), (
        "a failed close never guesses an outcome"
    )
    assert all(event.outcome_reason == expected_reason for event in output.events), (
        "the D13 reason order and vocabulary are preserved"
    )
    summary_type = outcome_module("outcome_records").WindowSummary
    has_summary = any(isinstance(record, summary_type) for record in output.records)
    assert has_summary is (failure == "mixed_window"), (
        "only a balanced mixed window persists H3 evidence"
    )
    if failure in {"ineligible_window", "mixed_window"}:
        progress = recorder_progress(state)
        assert progress.failure_count == 0, "a valid reading resets back-off"
        assert progress.last_failure is None, "a valid reading clears the failed cause"
        assert progress.next_read_at == pytest.approx(3.1), (
            "a valid reading uses the normal one-second interval"
        )


def test_query_deadline_precedes_a_late_read_error() -> None:
    """Apply D13's timeout precedence when a port reports after its deadline."""
    recorder = outcome_module("outcome_recorder")
    ports = outcome_module("outcome_ports")
    state, _ = begin_single_cached_query(recorder, cache_key(), 1.0)
    ticket = recorder_ticket(state)
    assert isinstance(ticket, ports.Ticket), "the query owns a bounded ticket"

    state, output = recorder.advance(
        state,
        recorder.PollResult(
            now=ticket.deadline,
            result=ports.QueryFailure("read_error"),
        ),
    )

    assert output.events[0].outcome_reason == "timeout", (
        "deadline expiry precedes a late transport error"
    )
    assert isinstance(state, recorder.Idle), "the expired query is terminal"


@pytest.mark.parametrize(
    ("before_name", "after_name", "expected_reason"),
    [
        ("none", "server", "generation_changed"),
        ("server", "none", "generation_changed"),
        ("none", "none", "generation_unavailable"),
        ("failure", "server", "generation_unavailable"),
        ("server", "failure", "generation_unavailable"),
    ],
    ids=(
        "server-started-during-read",
        "server-stopped-during-read",
        "server-absent-at-both-ends",
        "generation-read-failed-before-query",
        "generation-read-failed-after-query",
    ),
)
def test_generation_boundaries_distinguish_no_server_from_read_failure(
    before_name: str, after_name: str, expected_reason: str
) -> None:
    """Distinguish no-server transitions from failed identity reads."""
    recorder = outcome_module("outcome_recorder")
    ports = outcome_module("outcome_ports")
    values = {
        "server": GENERATION,
        "none": None,
        "failure": ports.GenerationFailure("generation_unavailable"),
    }
    state = state_with_window(
        recorder,
        WindowSetup(launch_payload(recorder, cache_key())),
    )
    state, _ = begin_query(
        state,
        recorder,
        clear_gate_event(recorder, 2.0),
        before_generation=values[before_name],
    )

    state, output = recorder.advance(
        state,
        recorder.PollResult(
            now=2.1,
            result=ports.StatisticsText(
                stats_text(compile_requests=1, requests_executed=1, cache_hits=1)
            ),
            after_generation=values[after_name],
        ),
    )

    assert output.reading_reasons == (expected_reason,), (
        "generation boundaries retain the D2 and RFC 0002 distinction"
    )
    assert output.events[0].outcome_reason == expected_reason, (
        "the named generation reason reaches each affected launch"
    )


def test_backoff_escalates_only_for_repeated_identical_failures() -> None:
    """Use 1 then 2 seconds for a repeat and reset on a different failure."""
    recorder, state, _ = begin_fresh_recorder_query()
    state, _ = recorder.advance(
        state,
        recorder.PollResult(
            now=0.1,
            result=outcome_module("outcome_ports").QueryFailure("timeout"),
        ),
    )
    first = recorder_progress(state)
    assert first.failure_count == 1, "the first timeout starts the retry count"
    assert first.next_read_at == pytest.approx(1.1), (
        "the first timeout waits one second"
    )

    state = _poll_retry_failure(state, recorder, 1.1, "timeout")
    repeated = recorder_progress(state)
    assert repeated.failure_count == 2, "a repeated timeout increments the count"
    assert repeated.next_read_at == pytest.approx(3.2), (
        "a repeated timeout waits two seconds"
    )

    state = _poll_retry_failure(state, recorder, 3.2, "read_error")
    changed = recorder_progress(state)
    assert changed.failure_count == 1, "a different failure starts a new sequence"
    assert changed.last_failure == "read_error", "the repeated cause is retained"
    assert changed.next_read_at == pytest.approx(4.3), "a new failure waits one second"


def test_backoff_remains_capped_after_a_long_failure_streak() -> None:
    """A long outage keeps the configured cap without overflowing exponentiation."""
    recorder, querying, _ = begin_fresh_recorder_query()
    previous_progress = recorder_progress(querying)
    progress = recorder.Progress(
        baseline=previous_progress.baseline,
        window=previous_progress.window,
        active=previous_progress.active,
        next_read_at=0.0,
        failure_count=1024,
        last_failure="timeout",
    )
    state, _ = begin_query(
        recorder.Idle(progress=progress), recorder, clear_gate_event(recorder, 1.0)
    )

    state, _ = recorder.advance(
        state,
        recorder.PollResult(
            now=1.1,
            result=outcome_module("outcome_ports").QueryFailure("timeout"),
        ),
    )

    updated = recorder_progress(state)
    assert updated.failure_count == 1025, "the failure streak remains observable"
    assert updated.next_read_at == pytest.approx(61.1), (
        "retry delay stays at 60 seconds"
    )
