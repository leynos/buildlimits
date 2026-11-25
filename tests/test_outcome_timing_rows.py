"""The row a completed reading writes, and its correlation evidence.

The supervisor-side production path is in
``test_outcome_timing_producer.py``; these tests drive the real reducer and
encoder directly, so one accepted window's row shape can be asserted without
a supervisor.
"""

import datetime as dt

import pytest
from outcome_recorder_test_helpers import (
    OBSERVED_AT,
    baseline_state,
    begin_query,
    cache_key,
    clear_gate_event,
    complete_query,
    stats_text,
)

from buildlimits.outcome_record_codec import decode_lines
from buildlimits.outcome_record_encode import encode_line
from buildlimits.outcome_record_format import (
    FORMAT_VERSION,
    DecodedStore,
    OutcomeRecord,
)
from buildlimits.outcome_recorder import Ended, Launched
from buildlimits.outcome_recorder_types import LaunchPayload

DECISION_AT = dt.datetime(2026, 11, 24, 9, 0, tzinfo=dt.UTC)


def _payload(generation: str | None) -> LaunchPayload:
    """Return one cached launch payload carrying a correlation identity."""
    return LaunchPayload(
        key=cache_key(),
        origin="registry",
        parser_class="cacheable",
        supervisor_generation=generation,
    )


def _window_output(
    *,
    launches: tuple[tuple[str, dt.datetime], ...],
    generation: str | None,
    observed_at: dt.datetime | None,
    stats: str | tuple[int, int, int],
) -> tuple[OutcomeRecord, ...]:
    """Drive one accepted window through the real reducer and return its rows.

    Parameters
    ----------
    launches : tuple[tuple[str, datetime], ...]
        One ``(group_id, completion)`` pair per cached launch in the window.
    generation : str | None
        The supervisor generation every launch carries, or None for a launch
        that predates that evidence.
    observed_at : datetime | None
        The moment the port returned the reading, or None when the caller
        supplied no wall clock.
    stats : str | tuple[int, int, int]
        The statistics text, or the triple ``(requests, executed, hits)``.

    Returns
    -------
    tuple[OutcomeRecord, ...]
        Every attributed outcome row the reading produced.
    """
    recorder = __import__("buildlimits.outcome_recorder", fromlist=["advance"])
    text = (
        stats
        if isinstance(stats, str)
        else stats_text(
            compile_requests=stats[0],
            requests_executed=stats[1],
            cache_hits=stats[2],
            compilations=stats[1] - stats[2],
            cache_misses=stats[1] - stats[2],
        )
    )
    state = baseline_state(recorder)
    for group_id, completion in launches:
        state, _ = recorder.advance(
            state,
            Launched(
                group_id=group_id,
                is_cached=True,
                payload=_payload(generation),
                now=1.0,
                admitted_at=DECISION_AT,
                observed_at=OBSERVED_AT,
            ),
        )
        state, _ = recorder.advance(
            state,
            Ended(group_id=group_id, is_clean=True, now=1.25, observed_at=completion),
        )
    state, _ = begin_query(state, recorder, clear_gate_event(recorder, 1.5))
    _state, output = complete_query(state, recorder, text, 1.6, observed_at=observed_at)
    return tuple(row for row in output.records if isinstance(row, OutcomeRecord))


def test_a_launch_row_carries_the_attempt_and_generation() -> None:
    """The recorded row names one attempt inside one supervisor generation."""
    (record,) = _window_output(
        launches=(("g7", OBSERVED_AT),),
        generation="generation-a",
        observed_at=OBSERVED_AT + dt.timedelta(seconds=45),
        stats=(1, 1, 1),
    )

    assert record.attempt == "g7", "the attempt is the admission group"
    assert record.supervisor_generation == "generation-a", "the clock domain is named"
    assert record.completed_at == OBSERVED_AT, "its own completion time is retained"
    assert record.observed_at == OBSERVED_AT + dt.timedelta(seconds=45), (
        "the row is dated at the poll boundary, not the launch's completion time"
    )
    assert record.label_time_provenance == "verified", (
        "and the row states that the time is proof"
    )
    assert record.admitted_at == DECISION_AT, "the decision time is unchanged"


def test_a_payload_without_a_generation_writes_no_correlation() -> None:
    """A launch that predates the generation evidence keeps the version 2 shape."""
    (record,) = _window_output(
        launches=(("g9", OBSERVED_AT),),
        generation=None,
        observed_at=OBSERVED_AT,
        stats=(1, 1, 1),
    )

    assert record.attempt is None, "no attempt is written without a generation"
    assert record.supervisor_generation is None, "nor a generation"
    assert record.observed_at == OBSERVED_AT, "the reading moment is still recorded"


def test_an_unpaired_poll_dates_the_row_at_the_launch_completion() -> None:
    """Without a paired wall clock the row falls back to its own completion."""
    (record,) = _window_output(
        launches=(("g8", OBSERVED_AT),),
        generation="generation-a",
        observed_at=None,
        stats=(1, 1, 1),
    )

    assert record.observed_at == OBSERVED_AT, (
        "the launch's own completion dates the row when no wall clock was "
        "supplied for the poll"
    )
    assert record.completed_at == OBSERVED_AT, "its own completion is retained"
    assert record.label_time_provenance is None, (
        "an unpaired poll proves no reading moment, so the row is unverified"
    )
    assert record.attempt == "g8", "the payload still names the invocation"
    assert record.supervisor_generation == "generation-a", (
        "and the generation whose clock dated it"
    )


def test_availability_is_never_taken_from_another_launch_in_the_window() -> None:
    """Two concurrent launches keep their own completions and one acceptance."""
    acceptance = OBSERVED_AT + dt.timedelta(seconds=30)
    slow_completion = OBSERVED_AT + dt.timedelta(seconds=20)
    fast_completion = OBSERVED_AT + dt.timedelta(seconds=5)

    rows = {
        row.attempt: row
        for row in _window_output(
            launches=(("slow", slow_completion), ("fast", fast_completion)),
            generation="generation-a",
            observed_at=acceptance,
            stats=(2, 2, 1),
        )
    }

    assert set(rows) == {"slow", "fast"}, "both attempts are retained separately"
    assert rows["slow"].completed_at == slow_completion, (
        "the slow attempt keeps its own"
    )
    assert rows["fast"].completed_at == fast_completion, "so does the fast one"
    assert all(row.observed_at == acceptance for row in rows.values()), (
        "one accepted reading dates every row of the window"
    )
    assert all(row.label_time_provenance == "verified" for row in rows.values()), (
        "and each row states that its time is proof"
    )


def _stored_record(
    *,
    generation: str = "generation-a",
    completed: dt.datetime | None = None,
) -> OutcomeRecord:
    """Return one complete version 3 record for an encode or decode case.

    Returns
    -------
    OutcomeRecord
        A record whose timing group is complete unless the caller overrides a
        field that makes it invalid.
    """
    return OutcomeRecord(
        key=cache_key(),
        outcome="hit",
        origin="registry",
        runtime_seconds=1.0,
        observed_at=OBSERVED_AT,
        parser_class="cacheable",
        child_sighting="unavailable",
        child_samples=0,
        admitted_at=OBSERVED_AT - dt.timedelta(seconds=9),
        completed_at=(
            OBSERVED_AT - dt.timedelta(seconds=3) if completed is None else completed
        ),
        label_time_provenance="verified",
        attempt="g1",
        supervisor_generation=generation,
    )


@pytest.mark.parametrize("generation", ["", "a" * 65, "bad/token", "bad token"])
def test_an_invalid_generation_is_refused_at_encode(generation: str) -> None:
    """A malformed generation cannot be persisted as if it were evidence."""
    with pytest.raises(ValueError, match="invalid"):
        encode_line(_stored_record(generation=generation))


def test_a_complete_timing_group_is_written_and_read_back() -> None:
    """The encoder writes the full group and the decoder restores it exactly."""
    record = _stored_record()
    header = (
        f'{{"format":"buildlimits-cache-outcomes","version":{FORMAT_VERSION},'
        '"salt_id":"s","written_at":"2026-01-01T00:00:00Z"}'
    )

    decoded = decode_lines(f"{header}\n{encode_line(record)}\n")

    assert isinstance(decoded, DecodedStore), "the document decodes"
    assert decoded.records == (record,), "every timing field round-trips exactly"
