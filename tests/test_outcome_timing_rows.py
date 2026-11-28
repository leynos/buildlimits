"""The row a completed reading writes, and its correlation evidence.

The supervisor-side production path is in
``test_outcome_timing_producer.py``; these tests drive the real reducer and
encoder directly, so one accepted window's row shape can be asserted without
a supervisor.
"""

import dataclasses as dc
import datetime as dt
import json

import pytest
from outcome_recorder_test_helpers import (
    OBSERVED_AT,
    baseline_state,
    begin_query,
    cache_key,
    clear_gate_event,
    complete_query,
    stats_text,
    stored_record,
)

from buildlimits.outcome_record_codec import decode_lines
from buildlimits.outcome_record_encode import encode_line
from buildlimits.outcome_record_format import (
    FORMAT_VERSION,
    DecodedStore,
    OutcomeRecord,
    RecordFailure,
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


@pytest.mark.parametrize("generation", ["", "a" * 65, "bad/token", "bad token"])
def test_an_invalid_generation_is_refused_at_encode(generation: str) -> None:
    """A malformed generation cannot be persisted as if it were evidence."""
    with pytest.raises(ValueError, match="invalid"):
        encode_line(stored_record(generation=generation))


@pytest.mark.parametrize("attempt", ["", "a" * 33, "bad/token", "bad token"])
def test_an_invalid_attempt_is_refused_at_encode(attempt: str) -> None:
    """The attempt half of the pair is bounded by the same contract.

    A generation-only test would pass if the encoder validated one half and
    copied the other, so each token is exercised on its own.
    """
    with pytest.raises(ValueError, match="invalid"):
        encode_line(dc.replace(stored_record(), attempt=attempt))


@pytest.mark.parametrize(
    ("attempt", "generation"),
    [("a" * 32, "b" * 64), ("g1", "gen-1")],
)
def test_the_longest_accepted_tokens_round_trip(attempt: str, generation: str) -> None:
    """Both bounds are inclusive, so the longest accepted pair is expressible."""
    record = dc.replace(stored_record(generation=generation), attempt=attempt)
    header = (
        f'{{"format":"buildlimits-cache-outcomes","version":{FORMAT_VERSION},'
        '"salt_id":"s","written_at":"2026-01-01T00:00:00Z"}'
    )

    decoded = decode_lines(f"{header}\n{encode_line(record)}\n")

    assert isinstance(decoded, DecodedStore), "the document decodes"
    assert decoded.records == (record,), "both tokens survive the bound"


@pytest.mark.parametrize(
    ("attempt", "generation"),
    [
        ("bad/token", "gen-1"),
        ("g1", ""),
        ("a" * 33, "gen-1"),
    ],
)
def test_a_version_five_row_with_a_bad_token_is_corrupt(
    attempt: str, generation: str
) -> None:
    """A written row cannot carry a token the decoder would refuse.

    The row is built by hand rather than through the encoder, so this pins the
    decoder's own `valid_fields` check: a document from a buggy or hostile
    writer must be reported as corrupt rather than read as a correlation.
    """
    row = json.loads(encode_line(stored_record()))
    row["attempt"] = attempt
    row["supervisor_generation"] = generation
    document = json.dumps(row)
    header = (
        f'{{"format":"buildlimits-cache-outcomes","version":{FORMAT_VERSION},'
        '"salt_id":"s","written_at":"2026-01-01T00:00:00Z"}'
    )

    decoded = decode_lines(f"{header}\n{document}\n")

    assert isinstance(decoded, RecordFailure), "the row is refused as corrupt"
    assert decoded.reason == "corrupt", "and names the closed reason"


def test_a_complete_timing_group_is_written_and_read_back() -> None:
    """The encoder writes the full group and the decoder restores it exactly."""
    record = stored_record()
    header = (
        f'{{"format":"buildlimits-cache-outcomes","version":{FORMAT_VERSION},'
        '"salt_id":"s","written_at":"2026-01-01T00:00:00Z"}'
    )

    decoded = decode_lines(f"{header}\n{encode_line(record)}\n")

    assert isinstance(decoded, DecodedStore), "the document decodes"
    assert decoded.records == (record,), "every timing field round-trips exactly"
