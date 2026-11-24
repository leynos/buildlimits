"""Store-row evidence for admission, overflow recovery and mixed windows."""

import dataclasses as dc
import datetime as dt
import tempfile
import typing as typ
from pathlib import Path

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
from predict_test_helpers import predict_config

from buildlimits.outcome_record_format import (
    DecodedStore,
    OutcomeRecord,
    StoreHeader,
    WindowSummary,
)
from buildlimits.predict.accounting import HistoryLoss
from buildlimits.predict.dataset import build_snapshot
from buildlimits.predict.harness import walk_forward
from buildlimits.predict.records import decode_history
from buildlimits.predict.snapshot_store import load_snapshot, save_snapshot

if typ.TYPE_CHECKING:
    from buildlimits.predict.events import Event

# A reading that returned an hour after the window's launches finished. The
# label cannot have existed before it, so every row the reading emits must
# carry this moment and never a launch's own completion time.
READING_AT = OBSERVED_AT + dt.timedelta(hours=1)
LAUNCH_COMPLETIONS = (
    OBSERVED_AT + dt.timedelta(minutes=3),
    OBSERVED_AT + dt.timedelta(minutes=1),
)


def _outcome_rows(records: tuple[object, ...]) -> list[OutcomeRecord]:
    """Return one reading's outcome rows, in the order it emitted them.

    Returns
    -------
    list[OutcomeRecord]
        The keyed outcome rows, excluding any window summary.
    """
    return [record for record in records if isinstance(record, OutcomeRecord)]


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
    recorder = outcome_module("outcome_recorder")
    state = baseline_state(recorder)
    for index, completed_at in enumerate(LAUNCH_COMPLETIONS):
        group_id = f"delayed-{index}"
        state, _ = recorder.advance(
            state,
            recorder.Launched(
                group_id=group_id,
                is_cached=True,
                payload=launch_payload(recorder, cache_key()),
                now=float(index),
                admitted_at=OBSERVED_AT,
                observed_at=completed_at,
            ),
        )
        state, _ = recorder.advance(
            state,
            recorder.Ended(
                group_id=group_id,
                is_clean=True,
                now=float(index) + 0.25,
                observed_at=completed_at,
            ),
        )
    state, _ = begin_query(state, recorder, clear_gate_event(recorder, 10.0))

    _state, output = complete_query(
        state,
        recorder,
        stats_text(compile_requests=2, requests_executed=2, cache_hits=2),
        10.1,
        observed_at=READING_AT,
    )

    rows = _outcome_rows(output.records)
    assert len(rows) == 2, "both launches are retained in the store"
    assert {row.observed_at for row in rows} == {READING_AT}, (
        "every row is dated at the reading that produced its label"
    )
    assert all(
        row.observed_at > completed for row in rows for completed in LAUNCH_COMPLETIONS
    ), "no row predates the query that produced its label"
    summary = next(
        record for record in output.records if not isinstance(record, OutcomeRecord)
    )
    assert typ.cast("WindowSummary", summary).observed_at == READING_AT, (
        "the summary is dated at the same reading moment"
    )


def _snapshot_events(records: list[OutcomeRecord]) -> tuple[Event, ...]:
    """Build a snapshot from one reading's rows and load it back.

    Returns
    -------
    tuple[Event, ...]
        The loaded event stream, in store append order.
    """
    decoded = DecodedStore(StoreHeader("fixed-salt", READING_AT), tuple(records))
    snapshot = build_snapshot(
        decode_history(decoded),
        now=READING_AT,
        history_loss=HistoryLoss.active_file_only(),
    )
    path = Path(tempfile.mkdtemp()) / "history.jsonl"
    save_snapshot(path, snapshot)
    return load_snapshot(path).events


def test_launches_that_finish_out_of_order_load_without_a_clock_refusal() -> None:
    """Reversed completions are not a backwards clock once rows share a reading.

    Before this fix each row carried its launch's own completion time, so two
    launches finishing out of order produced a snapshot the loader refused as a
    history whose clock ran backwards. Two rows from one reading now share its
    moment, so the store order is monotonic and the history scores.
    """
    recorder = outcome_module("outcome_recorder")
    state = baseline_state(recorder)
    for index, completed_at in enumerate(LAUNCH_COMPLETIONS):
        group_id = f"reversed-{index}"
        payload = launch_payload(
            recorder,
            dc.replace(cache_key(), argument_digest=f"{index:064x}"),
        )
        state, _ = recorder.advance(
            state,
            recorder.Launched(
                group_id=group_id,
                is_cached=True,
                payload=payload,
                now=float(index),
                admitted_at=OBSERVED_AT,
                observed_at=completed_at,
            ),
        )
        state, _ = recorder.advance(
            state,
            recorder.Ended(
                group_id=group_id,
                is_clean=True,
                now=float(index) + 0.25,
                observed_at=completed_at,
            ),
        )
    state, _ = begin_query(state, recorder, clear_gate_event(recorder, 10.0))
    _state, output = complete_query(
        state,
        recorder,
        stats_text(compile_requests=2, requests_executed=2, cache_hits=2),
        10.1,
        observed_at=READING_AT,
    )

    events = _snapshot_events(_outcome_rows(output.records))

    assert len(events) == 2, "both reversed completions reach the snapshot"
    assert events[0].observed_at == events[1].observed_at == READING_AT, (
        "one reading dates both rows, so store order is monotonic"
    )
    walk = walk_forward(events, predict_config())
    assert walk.moments, "the history is predictable rather than refused"


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

    # The overflowed groups were already emitted, so the recorder no longer
    # tracks them; their endings must still be tolerated, and the recovery
    # reading must follow the same completion sequence production would see.
    for index in range(65):
        state, _ = recorder.advance(
            state,
            recorder.Ended(
                group_id=f"overflow-{index}",
                is_clean=True,
                now=65.0 + index * 0.1,
                observed_at=OBSERVED_AT,
            ),
        )

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
