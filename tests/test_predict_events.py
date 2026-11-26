"""Admission-time and released-history contracts for the event stream.

Each fixture makes the verified label-availability time equal to the historical
observation time. That models a version 3 recorder that accepted the label at
the moment the old, completion-derived rows recorded, so a test still checks
the causality rule rather than the format upgrade.
"""

import dataclasses as dc
import datetime as dt
import typing as typ

import pytest

from buildlimits.predict.events import EPOCH, Event, admission_moment
from buildlimits.predict.history import features, label_batches
from buildlimits.predict.phases import phases
from buildlimits.predict.timing import audit

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import Origin
    from buildlimits.predict.events import Features, Label

ORIGIN = typ.cast("Origin", "registry")


def _causal(seconds: int, label: str, attempt: str | None = None) -> Event:
    """Return one event for identity ``a`` admitted five seconds before its label.

    A test that builds two events at the same instant passes distinct attempts,
    because two rows sharing one attempt identity would be a duplicate rather
    than two observations.
    """
    moment = EPOCH + dt.timedelta(seconds=seconds)
    return Event(
        "a" * 64,
        ORIGIN,
        EPOCH + dt.timedelta(seconds=seconds - 5),
        moment,
        typ.cast("Label", label),
        1.0,
        label_observed_verified=True,
        completed_at=moment,
        attempt=f"g{seconds}" if attempt is None else attempt,
        supervisor_generation="test-generation",
    )


def _admitted(identity: str, *, admitted: int, observed: int) -> Event:
    """Return one event with explicit admission and label times."""
    moment = EPOCH + dt.timedelta(seconds=observed)
    return Event(
        identity,
        ORIGIN,
        EPOCH + dt.timedelta(seconds=admitted),
        moment,
        "hit",
        1.0,
        label_observed_verified=True,
        completed_at=moment,
        attempt=f"g{identity[:4]}-{admitted}-{observed}",
        supervisor_generation="test-generation",
    )


def test_an_unknown_outcome_is_not_labelled_history() -> None:
    """An unknown outcome carries no usable label, so it is not history."""
    rows = features(
        (
            _causal(0, "hit"),
            _causal(10, "unknown"),
            _causal(20, "hit"),
            _causal(30, "hit"),
        ),
        load_window_seconds=60.0,
    )

    assert [typ.cast("Features", row).load for row in rows] == [0, 1, 1, 2], (
        "only the eligible hits are load"
    )
    assert typ.cast("Features", rows[2]).age_seconds == pytest.approx(15.0), (
        "the age skips the unknown outcome"
    )
    assert typ.cast("Features", rows[3]).age_seconds == pytest.approx(5.0), (
        "and measures to the last hit"
    )


def _third_row(events: tuple[Event, ...]) -> Features:
    """Return the third event's feature row from a three-event stream."""
    return typ.cast("Features", features(events, load_window_seconds=60.0)[2])


@pytest.mark.parametrize(
    ("events", "age", "load"),
    [
        pytest.param(
            (
                _admitted("a" * 64, admitted=0, observed=30),
                _admitted("b" * 64, admitted=1, observed=5),
                _admitted("c" * 64, admitted=10, observed=40),
            ),
            None,
            1,
            id="a-label-after-the-admission-is-not-load-for-it",
        ),
        pytest.param(
            (
                _admitted("a" * 64, admitted=0, observed=5),
                _admitted("b" * 64, admitted=6, observed=7),
                _admitted("a" * 64, admitted=10, observed=40),
            ),
            5.0,
            2,
            id="age-is-measured-from-the-admission-moment",
        ),
        pytest.param(
            (
                _admitted("a" * 64, admitted=0, observed=5),
                _admitted("a" * 64, admitted=8, observed=40),
                _admitted("a" * 64, admitted=12, observed=50),
            ),
            7.0,
            1,
            id="a-running-build-sets-no-age",
        ),
    ],
)
def test_features_read_only_the_released_history_before_the_admission(
    events: tuple[Event, ...], age: float | None, load: int
) -> None:
    """The third row sees exactly the labels released before its admission.

    Age is measured from the third row's admission to this identity's most
    recent released label, and load counts released labels strictly before
    that admission. A build still running when the row is measured has released
    nothing, so it sets neither an age for a later row nor load for this one.
    """
    row = _third_row(events)

    if age is None:
        assert row.age_seconds is None, "no released label sets an age"
    else:
        assert row.age_seconds == pytest.approx(age), "age from the admission"
    assert row.load == load, "only released labels before the moment are load"


def test_a_label_arriving_at_the_measurement_moment_sets_no_age() -> None:
    """A label observed at the moment itself is not an earlier label.

    The third row is admitted at exactly the first label's time. The first
    label is not released until that time has passed, so the row measures an
    empty history.
    """
    rows = features(
        (
            _causal(10, "hit"),
            _causal(20, "miss"),
            _admitted("a" * 64, admitted=10, observed=50),
        ),
        load_window_seconds=60.0,
    )

    assert typ.cast("Features", rows[2]).age_seconds is None, (
        "a tie at the moment is not earlier"
    )
    assert typ.cast("Features", rows[2]).load == 0, "age and load agree on the tie"


def test_load_counts_only_released_events_strictly_before_the_admission() -> None:
    """A tied label time is not load for an event admitted at the same instant."""
    rows = features(
        (
            _causal(0, "hit"),
            _causal(30, "hit"),
            _causal(60, "miss", attempt="g60-a"),
            _causal(60, "miss", attempt="g60-b"),
        ),
        load_window_seconds=60.0,
    )

    assert [typ.cast("Features", row).load for row in rows] == [0, 1, 2, 2], (
        "the tied pair cannot count each other"
    )


def test_a_zero_second_window_counts_no_load() -> None:
    """Nothing can occur strictly before a moment in no time at all."""
    events = (
        _causal(0, "hit"),
        _causal(1, "miss"),
        _admitted("a" * 64, admitted=60, observed=70),
    )

    narrow = features(events, load_window_seconds=0.0)
    wide = features(events, load_window_seconds=60.0)

    assert [typ.cast("Features", row).load for row in narrow] == [0, 0, 0], (
        "no window, no load"
    )
    assert typ.cast("Features", wide[2]).load == 2, (
        "the same labels are two minutes back"
    )


def test_a_load_window_before_the_earliest_moment_counts_nothing() -> None:
    """A year-one admission time has no earlier moment for a window to reach.

    Subtracting the window from the earliest representable timestamp would
    raise rather than count, failing the whole stream on one legal record. The
    window's start is clamped to that boundary, and nothing precedes it.
    """
    earliest = dt.datetime.min.replace(tzinfo=dt.UTC)
    event = Event(
        "d" * 64,
        ORIGIN,
        earliest,
        earliest,
        "hit",
        1.0,
        label_observed_verified=True,
        attempt="g0",
        supervisor_generation="test-generation",
    )

    rows = features((event,), load_window_seconds=60.0)

    assert [typ.cast("Features", row).load for row in rows] == [0], "no load"


def test_batches_follow_store_order_when_a_timestamp_steps_back() -> None:
    """A backward clock step never merges records from separate windows."""
    first = _causal(10, "hit")
    stepped_at = EPOCH + dt.timedelta(seconds=5)
    stepped = Event(
        "b" * 64,
        ORIGIN,
        EPOCH,
        stepped_at,
        "hit",
        1.0,
        label_observed_verified=True,
        completed_at=stepped_at,
        attempt="g-stepped",
        supervisor_generation="test-generation",
    )
    third = _causal(20, "miss")

    batches = label_batches((first, stepped, third))

    assert batches == ((0,), (1,), (2,)), "contiguity beats timestamp equality"
    rows = features((first, stepped, third), load_window_seconds=60.0)
    assert typ.cast("Features", rows[1]).load == 0, "the tied label is not earlier"
    assert typ.cast("Features", rows[2]).load == 2, "both earlier labels are history"


def test_an_unverified_row_does_not_split_an_equal_time_batch() -> None:
    """Two rows accepted at one reading share a batch across an unverified row.

    The unverified row has no availability, so it belongs to no batch. Letting
    it separate the two verified rows would place one of them in tuning and the
    other in the held-out period, which the split contract forbids.
    """
    first = _causal(10, "hit", attempt="g-first")
    unverified = Event(
        "b" * 64,
        ORIGIN,
        EPOCH,
        EPOCH + dt.timedelta(seconds=10),
        "hit",
        1.0,
        attempt="g-unverified",
        supervisor_generation="test-generation",
    )
    last = Event(
        "c" * 64,
        ORIGIN,
        EPOCH,
        EPOCH + dt.timedelta(seconds=10),
        "miss",
        1.0,
        label_observed_verified=True,
        attempt="g-last",
        supervisor_generation="test-generation",
    )

    assert label_batches((first, unverified, last)) == ((0, 2),), (
        "the unverified row joins no batch and separates nothing"
    )
    assigned = phases(
        (first, unverified, last),
        audit((first, unverified, last)),
        train_fraction=0.5,
    )
    assert assigned[0] == assigned[2], "one batch cannot straddle the split"
    assert assigned[1] is None, "an unverified row is in no period"


def test_a_rejected_duplicate_does_not_split_an_equal_time_batch() -> None:
    """A retried append is not evidence of a second reading.

    The duplicate carries the same attempt and evidence as the first row, so
    the audit rejects it. Letting its stamp close a batch would put the two
    rows accepted at the later reading on opposite sides of the split.
    """
    original = _causal(0, "hit", attempt="g-early")
    later = _causal(20, "miss", attempt="g-later")
    duplicate = original
    trailing = dc.replace(later, attempt="g-trailing")

    stream = (original, later, duplicate, trailing)
    verdict = audit(stream)
    assert verdict.reasons[2] == "duplicate_attempt", "the retry is rejected"

    batches = label_batches(stream, excluded=[r is not None for r in verdict.reasons])
    assert batches == ((0,), (1, 3)), "the two rows of the later reading share a batch"
    assigned = phases(stream, verdict, train_fraction=0.67)
    assert assigned[1] == assigned[3], "one batch cannot straddle the split"


@pytest.mark.parametrize(
    ("admitted", "observed", "expected"),
    [
        pytest.param(5, 10, EPOCH + dt.timedelta(seconds=5), id="before-its-label"),
        pytest.param(
            10, 10, EPOCH + dt.timedelta(seconds=10), id="tied-with-its-label"
        ),
        pytest.param(15, 10, None, id="after-its-label"),
        pytest.param(None, 10, None, id="missing"),
    ],
)
def test_only_an_admission_at_or_before_its_label_is_a_decision_time(
    admitted: int | None, observed: int, expected: dt.datetime | None
) -> None:
    """A missing or retroactive admission time never becomes a decision moment."""
    event = Event(
        "a" * 64,
        ORIGIN,
        None if admitted is None else EPOCH + dt.timedelta(seconds=admitted),
        EPOCH + dt.timedelta(seconds=observed),
        "hit",
        1.0,
        label_observed_verified=True,
    )

    assert admission_moment(event) == expected, (
        "only a recorded time at or before the label"
    )
