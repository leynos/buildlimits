"""State transitions for the pure cache-outcome recording service."""

import datetime as dt

from outcome_recorder_test_helpers import (
    GENERATION,
    OBSERVED_AT,
    baseline_state,
    begin_fresh_recorder_query,
    begin_query,
    begin_single_cached_query,
    cache_key,
    clear_gate_event,
    complete_query,
    launch_payload,
    outcome_module,
    recorder_progress,
    recorder_ticket,
    stats_reading,
    stats_text,
)

from buildlimits.outcome_record_format import OutcomeRecord


def test_zero_reading_establishes_baseline_without_label() -> None:
    """Accept a fresh all-zero sccache generation as the baseline."""
    attribution = outcome_module("outcome_attribution")
    recorder, state, start_output = begin_fresh_recorder_query()

    assert isinstance(state, recorder.Querying), "the start reply keeps the query open"
    assert isinstance(start_output.commands[0], recorder.StartQuery), (
        "a clear gate returns the statistics-port command"
    )
    state, output = complete_query(state, recorder, stats_text(), 0.1)

    assert isinstance(state, recorder.Idle), "a completed reading returns to Idle"
    assert recorder_progress(state).baseline == attribution.Reading(
        GENERATION, stats_reading()
    ), "the fresh generation becomes the baseline"
    assert output.events == (), "an empty baseline creates no per-launch event"
    assert output.reading_reasons == ("none",), "the baseline was accepted"


def test_a_solo_window_emits_one_label_record_and_summary() -> None:
    """Close one balanced window and emit its typed outcome row."""
    recorder = outcome_module("outcome_recorder")
    state, _ = begin_single_cached_query(recorder, cache_key(), 1.5)

    state, output = complete_query(
        state,
        recorder,
        stats_text(compile_requests=1, requests_executed=1, cache_hits=1),
        1.6,
    )

    assert isinstance(state, recorder.Idle), "a completed window returns to Idle"
    assert len(output.events) == 1, "one cached launch produces one event"
    assert output.events[0].cache_result == "hit", "the footprint labels the hit"
    assert output.events[0].outcome_attribution == "solo", (
        "one request is labelled from its solo window"
    )
    assert output.events[0].outcome_reason == "none", "a sound label has no reason"
    assert len(output.records) == 2, "the outcome and window summary are persisted"
    assert output.records[0].key == cache_key(), "the record retains the keyed identity"
    assert output.records[0].outcome_reason == "none", "a label has no refusal reason"
    assert output.records[0].outcome_attribution == "solo", (
        "the store row keeps the attribution that produced the label"
    )
    assert recorder_progress(state).window.entries == (), (
        "the accepted window is consumed once"
    )
    assert recorder_progress(state).active == (), (
        "closed launches leave the retained active set"
    )


def test_restart_reading_is_rejected_then_used_as_the_new_baseline() -> None:
    """Reject a generation change, then anchor the next window to that reading."""
    attribution = outcome_module("outcome_attribution")
    recorder = outcome_module("outcome_recorder")
    ports = outcome_module("outcome_ports")
    state, _ = begin_single_cached_query(recorder, cache_key(), 1.5)
    next_generation = attribution.Generation("run-b", 11, 21)

    state, rejected = recorder.advance(
        state,
        recorder.PollResult(
            now=1.6,
            result=ports.StatisticsText(
                stats_text(compile_requests=1, requests_executed=1, cache_hits=1)
            ),
            after_generation=next_generation,
        ),
    )

    assert rejected.events[0].cache_result == "unattributed", (
        "the restart window receives no inferred label"
    )
    assert rejected.events[0].outcome_reason == "generation_changed", (
        "the restart reason remains visible"
    )
    new_baseline = recorder_progress(state).baseline
    assert new_baseline is not None, "the restart reading establishes a baseline"
    assert new_baseline.generation == next_generation, (
        "the reading that exposed the restart becomes the next baseline"
    )


def test_generation_change_during_straddled_reading_does_not_set_baseline() -> None:
    """A changed generation cannot make a straddled reading a baseline."""
    attribution = outcome_module("outcome_attribution")
    recorder = outcome_module("outcome_recorder")
    ports = outcome_module("outcome_ports")
    next_generation = attribution.Generation("run-b", 11, 21)
    state, _ = begin_query(
        baseline_state(recorder), recorder, clear_gate_event(recorder, 0.0)
    )
    state, _ = recorder.advance(
        state,
        recorder.Launched(
            group_id="straddled-after-restart",
            is_cached=True,
            payload=launch_payload(recorder, cache_key()),
            now=0.5,
        ),
    )

    state, output = recorder.advance(
        state,
        recorder.PollResult(
            now=0.75,
            result=ports.StatisticsText(
                stats_text(compile_requests=1, requests_executed=1, cache_hits=1)
            ),
            after_generation=next_generation,
        ),
    )

    assert output.reading_reasons == ("generation_changed",), (
        "the generation change remains the primary refusal reason"
    )
    assert output.events[0].outcome_reason == "generation_changed", (
        "the straddled launch remains unattributed across the restart"
    )
    assert recorder_progress(state).baseline is None, (
        "a reading that straddles a launch cannot seed the new generation"
    )


def test_straddled_reading_is_neither_window_end_nor_baseline() -> None:
    """Keep the old baseline and buffered window when a launch straddles a read."""
    recorder = outcome_module("outcome_recorder")
    old_baseline = recorder_progress(baseline_state(recorder)).baseline
    state, _ = begin_query(
        baseline_state(recorder), recorder, clear_gate_event(recorder, 0.0)
    )
    state, _ = recorder.advance(
        state,
        recorder.Launched(
            group_id="straddled",
            is_cached=True,
            payload=launch_payload(recorder, cache_key()),
            now=0.5,
        ),
    )

    state, output = complete_query(
        state,
        recorder,
        stats_text(compile_requests=1, requests_executed=1, cache_hits=1),
        0.75,
    )

    assert output.events == (), "the in-flight window is not labelled"
    assert output.reading_reasons == ("straddled_reading",), (
        "the rejected reading names the straddle"
    )
    assert recorder_progress(state).baseline == old_baseline, (
        "the old reading remains baseline"
    )
    assert len(recorder_progress(state).window.entries) == 1, (
        "the launch remains in the window"
    )


def test_window_overflow_emits_unattributed_events_once_and_stays_bounded() -> None:
    """Emit every launch as unattributed when the 64-entry window overflows."""
    recorder = outcome_module("outcome_recorder")
    payload = launch_payload(recorder, cache_key())
    state = recorder.Idle()
    emitted = []
    for index in range(65):
        state, output = recorder.advance(
            state,
            recorder.Launched(
                group_id=f"group-{index}",
                is_cached=True,
                payload=payload,
                now=float(index),
            ),
        )
        emitted.extend(output.events)

    assert len(state.progress.window.entries) <= 64, "the ledger remains bounded"
    assert len(emitted) == 65, "overflow does not lose or duplicate a launch"
    assert all(event.cache_result == "unattributed" for event in emitted), (
        "overflowed launches are never guessed"
    )
    assert all(event.outcome_reason == "window_overflow" for event in emitted), (
        "the overflow reason is named"
    )


def test_a_label_record_carries_the_admission_moment_from_its_launch() -> None:
    """The admission time is copied from the launch, never derived later."""
    recorder = outcome_module("outcome_recorder")
    admitted_at = dt.datetime(2026, 10, 24, 11, 59, tzinfo=dt.UTC)
    state = baseline_state(recorder)
    state, _ = recorder.advance(
        state,
        recorder.Launched(
            group_id="admitted",
            is_cached=True,
            payload=launch_payload(recorder, cache_key()),
            now=1.0,
            admitted_at=admitted_at,
        ),
    )
    state, _ = recorder.advance(
        state,
        recorder.Ended(
            group_id="admitted",
            is_clean=True,
            now=1.25,
            observed_at=OBSERVED_AT,
        ),
    )
    state, _ = begin_query(state, recorder, clear_gate_event(recorder, 1.5))

    _state, output = complete_query(
        state,
        recorder,
        stats_text(compile_requests=1, requests_executed=1, cache_hits=1),
        1.6,
    )

    record = output.records[0]
    assert isinstance(record, OutcomeRecord), record
    assert record.admitted_at == admitted_at, "the row carries the launch's time"
    assert record.observed_at == OBSERVED_AT, "the label time is unchanged"
    assert record.admitted_at <= record.observed_at, (
        "the decision moment precedes the label it produced"
    )


def test_window_overflow_resets_the_baseline_for_the_next_window() -> None:
    """Overflow is bounded loss, not a permanently unbalanced history.

    The overflowed launches are still in sccache's counters. Keeping the old
    baseline would make every later delta include requests that are no longer
    in the window, so the next reading must establish a fresh baseline and the
    window after that must be labelable again.
    """
    recorder = outcome_module("outcome_recorder")
    payload = launch_payload(recorder, cache_key())
    state = baseline_state(recorder)
    for index in range(65):
        state, _ = recorder.advance(
            state,
            recorder.Launched(
                group_id=f"overflow-{index}",
                is_cached=True,
                payload=payload,
                now=float(index),
            ),
        )

    overflowed = recorder_progress(state)
    assert overflowed.baseline is None, "the stale baseline is dropped"
    assert overflowed.window.entries == (), "the overflowed window is cleared"

    state, _ = begin_query(state, recorder, clear_gate_event(recorder, 100.0))
    state, baseline = complete_query(
        state,
        recorder,
        stats_text(compile_requests=65, requests_executed=65, cache_hits=65),
        100.1,
    )
    assert baseline.reading_reasons == ("none",), "the fresh baseline is accepted"
    assert recorder_progress(state).baseline is not None, "a baseline is restored"

    state, _ = recorder.advance(
        state,
        recorder.Launched(
            group_id="after-overflow",
            is_cached=True,
            payload=payload,
            now=101.0,
            admitted_at=OBSERVED_AT,
        ),
    )
    state, _ = recorder.advance(
        state,
        recorder.Ended(
            group_id="after-overflow",
            is_clean=True,
            now=101.25,
            observed_at=OBSERVED_AT,
        ),
    )
    state, _ = begin_query(state, recorder, clear_gate_event(recorder, 102.0))
    _state, output = complete_query(
        state,
        recorder,
        stats_text(compile_requests=66, requests_executed=66, cache_hits=66),
        102.1,
    )

    assert output.events[0].cache_result == "hit", (
        "the window after an overflow can still be labelled"
    )
    assert output.events[0].outcome_reason == "none", "the label is sound"
    recovered = output.records[0]
    assert isinstance(recovered, OutcomeRecord), recovered
    assert recovered.admitted_at == OBSERVED_AT, (
        "the recovered window still records its admission time"
    )


def test_a_mixed_window_records_its_reason_for_both_rows() -> None:
    """A balanced window the counters cannot split stays unknown, with a reason."""
    recorder = outcome_module("outcome_recorder")
    state = baseline_state(recorder)
    for index, group_id in enumerate(("mixed-hit", "mixed-miss")):
        state, _ = recorder.advance(
            state,
            recorder.Launched(
                group_id=group_id,
                is_cached=True,
                payload=launch_payload(recorder, cache_key()),
                now=float(index),
            ),
        )
        state, _ = recorder.advance(
            state,
            recorder.Ended(
                group_id=group_id,
                is_clean=True,
                now=float(index) + 0.25,
                observed_at=OBSERVED_AT,
            ),
        )
    state, _ = begin_query(state, recorder, clear_gate_event(recorder, 10.0))

    _state, output = complete_query(
        state,
        recorder,
        stats_text(
            compile_requests=2,
            requests_executed=2,
            cache_hits=1,
            compilations=1,
            cache_misses=1,
        ),
        10.1,
    )

    records = [record for record in output.records if isinstance(record, OutcomeRecord)]
    assert len(records) == 2, "both launches are retained in the store"
    for record in records:
        assert record.outcome == "unattributed", "a mixed window is never split"
        assert record.outcome_reason == "mixed_window", "the refusal is named"
        assert record.outcome_attribution == "none", "no attribution is invented"


def test_stopping_closes_a_query_and_flushes_open_launches() -> None:
    """Close a live ticket and account for launches still buffered at stop."""
    recorder = outcome_module("outcome_recorder")
    ports = outcome_module("outcome_ports")
    recorder, state, _ = begin_fresh_recorder_query()
    state, _ = recorder.advance(
        state,
        recorder.Launched(
            group_id="open-at-stop",
            is_cached=True,
            payload=launch_payload(recorder, cache_key()),
            now=0.5,
        ),
    )
    ticket = recorder_ticket(state)

    state, output = recorder.advance(
        state, recorder.Stopping(now=1.0, observed_at=OBSERVED_AT)
    )

    assert isinstance(state, recorder.Disabled), "a stopped recorder is terminal"
    assert output.commands == (recorder.CloseQuery(ticket),), (
        "the in-flight statistics ticket is closed"
    )
    assert len(output.events) == 1, "the launch is accounted exactly once"
    assert output.events[0].outcome_reason == "supervisor_stopped", (
        "stop names why the window cannot be attributed"
    )
    assert isinstance(ticket, ports.Ticket), "close receives the opaque port ticket"
