"""Summarized drain successes follow the last written record, not the clock.

The summary answers one question for every record the drain observer offers
it: does this record add anything to the one already written? These properties
state the rule over generated histories, because the interesting mistakes are
orderings rather than single examples: an interval that runs from the last
attempt instead of the last written record, a failure that fails to restart the
interval, or a second operation sharing another operation's interval.
"""

import math
import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from buildlimits.drain_metrics import DrainMetrics
from buildlimits.drain_summary import SuccessSummary
from buildlimits.metrics import Metrics

OPERATIONS = ("terminate", "empty_check")
STATES = ("completed", "failed", "pending", "recovered")
INTERVAL = 600.0

INTERVALS = st.floats(
    min_value=0.001, max_value=1200.0, allow_nan=False, allow_infinity=False
)
ADVANCES = st.floats(
    min_value=0.0, max_value=1200.0, allow_nan=False, allow_infinity=False
)
STEPS = st.lists(
    st.tuples(st.sampled_from(OPERATIONS), st.sampled_from(STATES), ADVANCES),
    max_size=25,
)


def _raise(error: Exception) -> typ.Never:
    """Raise the supplied object to model a failed drain action."""
    raise error


@given(interval=INTERVALS, steps=STEPS)
def test_only_a_completed_repeating_the_last_written_record_is_summarized(
    interval: float, steps: list[tuple[str, str, float]]
) -> None:
    """A success is a repeat only after a recent written success of its operation.

    Parameters
    ----------
    interval : float
        The minimum seconds between written records of repeated successes.
    steps : list[tuple[str, str, float]]
        One ``(operation, state, clock advance)`` triple per offer, in order.

    Notes
    -----
    The model keeps only the two facts the rule is stated in: each operation's
    last written state and when that record was written. As in production, a
    summarized repeat writes nothing, so it does not restart the interval. The
    clock advances monotonically, as ``time.monotonic`` does.
    """
    now = [0.0]
    summary = SuccessSummary(clock=lambda: now[0], interval=interval)
    last_state: dict[str, str] = {}
    last_written: dict[str, float] = {}
    for operation, state, advance in steps:
        now[0] += advance
        elapsed = now[0] - last_written.get(operation, -math.inf)
        expected = (
            state == "completed"
            and last_state.get(operation) == "completed"
            and elapsed < interval
        )
        assert summary.is_repeat(operation, state) is expected, (
            f"{operation} {state} after {elapsed}s since its last written record"
        )
        if not expected:
            summary.recorded(operation, state)
            last_state[operation] = state
            last_written[operation] = now[0]


@given(
    steps=st.lists(
        st.tuples(st.sampled_from(OPERATIONS), st.booleans(), ADVANCES),
        max_size=20,
    )
)
def test_drain_metrics_writes_exactly_the_successes_the_summary_keeps(
    steps: list[tuple[str, bool, float]],
) -> None:
    """Every call counts, and only a success that repeats the last record is quiet.

    Parameters
    ----------
    steps : list[tuple[str, bool, float]]
        One ``(operation, failed, clock advance)`` triple per drain action, in
        the order the supervisor would run them.
    """
    from conftest import RecordingSink

    now = [0.0]
    sink = RecordingSink()
    observer = DrainMetrics(
        Metrics(sink), summary=SuccessSummary(clock=lambda: now[0], interval=INTERVAL)
    )
    last_state: dict[str, str] = {}
    last_written: dict[str, float] = {}
    faulted: dict[str, bool] = {}
    attempts: dict[str, int] = dict.fromkeys(OPERATIONS, 0)
    failures: dict[str, int] = dict.fromkeys(OPERATIONS, 0)
    recoveries: dict[str, int] = dict.fromkeys(OPERATIONS, 0)
    written: list[tuple[str, str]] = []
    for operation, failed, advance in steps:
        now[0] += advance
        attempts[operation] += 1
        if failed:
            failures[operation] += 1
            faulted[operation] = True
            with pytest.raises(OSError, match=r"^private$"):
                observer.call(operation, lambda: _raise(OSError("private")))
            state = "failed"
        else:
            state = "recovered" if faulted.get(operation) else "completed"
            recoveries[operation] += int(faulted.get(operation, False))
            faulted[operation] = False
            observer.call(operation, lambda: None)
        repeat = (
            state == "completed"
            and last_state.get(operation) == "completed"
            and now[0] - last_written.get(operation, -math.inf) < INTERVAL
        )
        if not repeat:
            written.append((operation, state))
            last_state[operation] = state
            last_written[operation] = now[0]
        assert [
            (record["BUILD_LIMITS_DRAIN_OPERATION"], record["BUILD_LIMITS_DRAIN_STATE"])
            for record in sink.records
        ] == written, "a success is written exactly when the summary keeps it"
        if repeat:
            continue
        record = sink.records[-1]
        assert int(record["BUILD_LIMITS_DRAIN_ATTEMPTS"]) == attempts[operation], (
            "the record counts every attempt of its own operation"
        )
        assert int(record["BUILD_LIMITS_DRAIN_FAILURES"]) == failures[operation], (
            "the record counts every fault of its own operation"
        )
        assert int(record["BUILD_LIMITS_DRAIN_RECOVERIES"]) == recoveries[operation], (
            "the record counts every recovery of its own operation"
        )
