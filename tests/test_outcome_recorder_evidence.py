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
