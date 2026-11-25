"""Store-row evidence for admission, overflow recovery and mixed windows."""

import datetime as dt

from outcome_reading_test_helpers import (
    DELAYED_READING_AT,
    LAUNCH_COMPLETIONS,
    delayed_reading_rows,
    outcome_rows,
)
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

from buildlimits.outcome_record_format import (
    OutcomeRecord,
    WindowSummary,
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
    assert record.admitted_at is not None, "the row carries an admission time"
    assert record.admitted_at == admitted_at, "the row carries the launch's time"
    assert record.observed_at == OBSERVED_AT, "the reading moment is the label time"
    assert record.admitted_at <= record.observed_at, (
        "the decision moment precedes the label it produced"
    )


def test_a_label_is_not_released_before_its_query_completed() -> None:
    """A delayed reading dates the label at its own completion, not at the end.

    The launches finish an hour before the statistics query returns. sccache
    exposes no label until that query is read, so a row whose ``observed_at``
    were the launch's completion would let a prediction admitted in between
    learn a label that had not been observed yet. Every row of the reading,
    including the window summary, must carry the later moment.
    """
    records = delayed_reading_rows(
        completions=LAUNCH_COMPLETIONS, reading_at=DELAYED_READING_AT
    )

    rows = outcome_rows(records)
    assert len(rows) == 2, "both launches are retained in the store"
    assert {row.observed_at for row in rows} == {DELAYED_READING_AT}, (
        "every row is dated at the reading that produced its label"
    )
    assert all(
        row.observed_at > completed for row in rows for completed in LAUNCH_COMPLETIONS
    ), "no row predates the query that produced its label"
    summary = next(
        record for record in records if not isinstance(record, OutcomeRecord)
    )
    assert isinstance(summary, WindowSummary), "the second record is the summary"
    assert summary.observed_at == DELAYED_READING_AT, (
        "the summary is dated at the same reading moment"
    )


def test_an_overflow_resets_the_baseline_and_a_later_hit_is_attributed() -> None:
    """An overflowing window discards the baseline, so the next one can balance.

    The overflowed requests remain in sccache's cumulative counters, so keeping
    the old baseline would make every later window's delta include requests it
    does not hold. The reset is what lets a later reading attribute a solo hit
    at all; without it, this test's final label never appears.
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

    assert recorder_progress(state).baseline is None, (
        "the overflowed window cannot keep a baseline it no longer balances"
    )

    state, _ = begin_query(state, recorder, clear_gate_event(recorder, 100.0))
    state, _ = complete_query(state, recorder, stats_text(), 100.5)
    assert recorder_progress(state).baseline is not None, (
        "the next reading establishes a fresh baseline"
    )

    state, _ = recorder.advance(
        state,
        recorder.Launched(
            group_id="after-overflow",
            is_cached=True,
            payload=payload,
            now=101.0,
        ),
    )
    state, _ = recorder.advance(
        state,
        recorder.Ended(
            group_id="after-overflow", is_clean=True, now=101.5, observed_at=OBSERVED_AT
        ),
    )
    state, _ = begin_query(state, recorder, clear_gate_event(recorder, 102.0))
    _state, output = complete_query(
        state,
        recorder,
        stats_text(compile_requests=1, requests_executed=1, cache_hits=1),
        102.5,
    )

    rows = [record for record in output.records if isinstance(record, OutcomeRecord)]

    assert len(rows) == 1, "the later launch is attributed on its own"
    assert rows[0].outcome == "hit", "a solo hit is labelled rather than guessed"
    assert rows[0].outcome_attribution == "solo", "the attribution is the solo case"
    assert rows[0].outcome_reason == "none", "a label carries no refusal reason"
