"""Store-row evidence for admission, overflow recovery and mixed windows."""

import datetime as dt

from outcome_recorder_test_helpers import (
    OBSERVED_AT,
    baseline_state,
    begin_query,
    cache_key,
    clear_gate_event,
    complete_query,
    launch_payload,
    outcome_module,
    recorder_progress,
    stats_text,
)

from buildlimits.outcome_record_format import OutcomeRecord


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
    assert record.admitted_at is not None, "the row carries an admission time"
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
