"""The producer's decision boundary and its correlation evidence.

The supervisor's launch handler is the first code that runs after admission
granted a token and decided to launch the group. These tests pin where the
recorded decision moment is read, that a later wall-clock read cannot stand in
for it, and that the recorder writes one bounded, path-free correlation
identity per supervisor process.

Every clock is injected: the monotonic clock is a value the test advances, and
the wall clock returns whatever the test sets. That makes the ordering the
tests assert a property of the code rather than of the host's timing.
"""

import datetime as dt
import selectors
import typing as typ
from pathlib import Path

import pytest
from groups_helpers import FakeGroups
from outcome_recorder_test_helpers import (
    OBSERVED_AT,
    baseline_state,
    begin_query,
    cache_key,
    clear_gate_event,
    complete_query,
    stats_text,
)
from request_helpers import crate_pending
from supervisor_helpers import supervisor

from buildlimits.config import SupervisorConfig
from buildlimits.groups import Groups
from buildlimits.outcome_record_codec import decode_lines
from buildlimits.outcome_record_encode import encode_line
from buildlimits.outcome_record_format import DecodedStore, OutcomeRecord
from buildlimits.outcome_recorder import Ended, Launched
from buildlimits.outcome_recorder_types import LaunchPayload
from buildlimits.supervisor_outcome_setup import mint_generation

if typ.TYPE_CHECKING:
    from buildlimits.pending_request import Pending
    from buildlimits.supervisor import Supervisor

__all__ = ["supervisor"]  # Re-exported so pytest finds the fixture here.

DECISION_AT = dt.datetime(2026, 11, 24, 9, 0, tzinfo=dt.UTC)
LATER_AT = DECISION_AT + dt.timedelta(minutes=5)


def _admitted_group(supervisor: Supervisor, *, clock: list[float]) -> Pending:
    """Register one request whose group admission already placed.

    Returns
    -------
    Pending
        The admitted request the launch handler will read.
    """
    from buildlimits.admission import Group, GroupState

    supervisor.clock = lambda: clock[0]
    entry = crate_pending(1)
    entry.admitted = None
    entry.admitted_at = None
    supervisor.requests.pending[1] = entry
    supervisor.admission.groups["g1"] = Group(
        "g1", request_id=1, token=b"a", cached=False, state=GroupState.RUNNING
    )
    return entry


def test_a_queue_delay_does_not_move_the_recorded_decision(
    supervisor: Supervisor,
) -> None:
    """The decision is the token grant, not request arrival or queue entry.

    A request can arrive and wait far behind other work. The recorded moment
    must be the one where admission granted the token, so a long queue wait
    never makes a decision look earlier than it was.
    """
    arrived_at = DECISION_AT - dt.timedelta(minutes=30)
    clock = [1800.0]
    supervisor.groups = typ.cast(
        "Groups", FakeGroups(order=[], placed=None, terminate=lambda _g: None)
    )
    supervisor.groups.wall_clock = lambda: DECISION_AT
    entry = _admitted_group(supervisor, clock=clock)
    entry.arrived = 0.0

    supervisor._launch(1, "g1", cached=False)

    assert entry.admitted_at == DECISION_AT, (
        "the decision moment is the token grant, not the arrival time"
    )
    assert entry.admitted == pytest.approx(1800.0), (
        "the monotonic decision time is the grant, not zero"
    )
    assert entry.arrived == pytest.approx(0.0), "the arrival time is unchanged"
    assert entry.admitted_at != arrived_at, "a queue wait cannot shift the decision"


def test_the_decision_moment_is_read_before_any_placement_work(
    supervisor: Supervisor,
) -> None:
    """A clock that advances during placement cannot move the recorded moment."""
    observed: list[tuple[float, dt.datetime]] = []
    clock = [0.0]

    def record_and_advance() -> None:
        """Note both clocks, then move them on as placement would."""
        observed.append((supervisor.clock(), supervisor.groups.wall_clock()))
        clock[0] += 10.0

    supervisor.groups = typ.cast(
        "Groups",
        FakeGroups(order=[], placed=None, on_place=record_and_advance),
    )
    supervisor.groups.wall_clock = lambda: DECISION_AT + dt.timedelta(seconds=clock[0])
    entry = _admitted_group(supervisor, clock=clock)

    supervisor._launch(1, "g1", cached=False)

    assert observed == [(0.0, DECISION_AT)], (
        "both clocks were read at the decision, before placement advanced them"
    )
    assert entry.admitted == pytest.approx(0.0), (
        "the monotonic decision time is the first read"
    )
    assert entry.admitted_at == DECISION_AT, (
        "the UTC decision time is the decision moment, not the later placement"
    )


def test_placement_never_restamps_an_already_marked_decision() -> None:
    """A marked entry keeps its moment even if placement's clock has moved on."""
    selector = selectors.DefaultSelector()
    try:
        groups = Groups(
            SupervisorConfig(), typ.cast("typ.Any", object()), selector, lambda: 99.0
        )
        groups.wall_clock = lambda: LATER_AT
        entry = crate_pending(1)

        # The slice is unset, so placement refuses before spawning or scoping;
        # what matters is that it did not overwrite the recorded decision.
        with pytest.raises(RuntimeError):
            groups.place(entry, "g1", cached=False)

        assert entry.admitted is None, "no placement fill happened for a refusal"
        assert entry.admitted_at is None, "and no wall clock was read"
    finally:
        selector.close()


def test_placement_fills_an_unmarked_entry_before_its_fork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct caller that marks nothing still records a pre-execution time."""
    from buildlimits import launch

    def exhausted(_spec: object) -> None:
        """Fail as a fork does when the process limit is reached.

        Raises
        ------
        OSError
            Always, standing in for resource exhaustion at fork.
        """
        raise OSError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(launch, "spawn_held", exhausted)
    selector = selectors.DefaultSelector()
    try:
        groups = Groups(
            SupervisorConfig(), typ.cast("typ.Any", object()), selector, lambda: 7.0
        )
        groups.wall_clock = lambda: DECISION_AT
        groups.slice_dir = Path("/sys/fs/cgroup/compilers.slice")
        entry = crate_pending(1)

        assert groups.place(entry, "g1", cached=False) is None, (
            "the child cannot actually spawn in this test"
        )
        assert entry.admitted == pytest.approx(7.0), (
            "the monotonic decision time was filled in"
        )
        assert entry.admitted_at == DECISION_AT, "and so was the UTC moment"
    finally:
        selector.close()


def test_a_minted_generation_is_bounded_and_path_free() -> None:
    """The supervisor generation is opaque, bounded and distinct per process."""
    first = mint_generation()
    second = mint_generation()

    assert 0 < len(first) <= 64, "the generation fits its recorded bound"
    assert first != second, "a fresh supervisor gets a fresh clock domain"
    assert "/" not in first and " " not in first, "it reveals no path or space"


def _payload(generation: str) -> LaunchPayload:
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
    _state, output = complete_query(
        state, recorder, text, 1.6, observed_at=observed_at or OBSERVED_AT
    )
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
    assert record.observed_at == record.label_available_at, (
        "the row's observation time is the reading that justified it"
    )
    assert record.label_available_at == OBSERVED_AT + dt.timedelta(seconds=45), (
        "availability is the poll boundary, not the launch's completion time"
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


def test_an_unpaired_poll_still_dates_the_row_at_its_reading() -> None:
    """A reading with no paired wall clock still dates its row at itself."""
    (record,) = _window_output(
        launches=(("g8", OBSERVED_AT),),
        generation="generation-a",
        observed_at=None,
        stats=(1, 1, 1),
    )

    assert record.label_available_at == OBSERVED_AT, (
        "the reading's own moment is the availability, even when no wall clock "
        "was supplied for the poll"
    )
    assert record.completed_at == OBSERVED_AT, "its own completion is retained"
    assert record.observed_at == record.label_available_at, (
        "the row's observation time and its availability agree"
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
    assert all(row.label_available_at == acceptance for row in rows.values()), (
        "one accepted reading gives every row of the window the same availability"
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
        label_available_at=OBSERVED_AT,
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
        '{"format":"buildlimits-cache-outcomes","version":3,'
        '"salt_id":"s","written_at":"2026-01-01T00:00:00Z"}'
    )

    decoded = decode_lines(f"{header}\n{encode_line(record)}\n")

    assert isinstance(decoded, DecodedStore), "the document decodes"
    assert decoded.records == (record,), "every timing field round-trips exactly"


# weave: run 'weave explain tests/test_outcome_timing_producer.py' for per-hunk detail, 'weave check' to verify your resolution
# weave: run 'weave explain tests/test_outcome_timing_producer.py' for per-hunk detail, 'weave check' to verify your resolution
