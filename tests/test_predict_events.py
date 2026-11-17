"""Snapshot-line validation contracts for the event stream."""

import datetime as dt
import typing as typ

import pytest

from buildlimits.predict.events import EPOCH, Event, admission_moment, event_from_dict
from buildlimits.predict.history import features, label_batches

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import Origin
    from buildlimits.predict.events import Features, Label

ORIGIN = typ.cast("Origin", "registry")


def _line(**changes: object) -> dict[str, object]:
    """Return a valid encoded event with ``changes`` applied."""
    event = Event("a" * 64, ORIGIN, EPOCH, EPOCH, "hit", 0.25)
    line = event.to_dict()
    line.update(changes)
    return line


def test_an_encoded_event_round_trips() -> None:
    """Encoding then decoding returns the same event."""
    event = Event("b" * 64, ORIGIN, EPOCH, EPOCH, "miss", 1.5)

    assert event_from_dict(event.to_dict()) == event, "fields survive the trip"


def test_a_line_that_is_not_an_object_is_a_type_error() -> None:
    """A JSON array cannot be an event."""
    with pytest.raises(TypeError, match="must be a JSON object"):
        event_from_dict(["identity"])


@pytest.mark.parametrize(
    "line",
    [
        pytest.param({"identity": "a" * 64}, id="missing-fields"),
        pytest.param({**_line(), "extra": 1}, id="extra-field"),
    ],
)
def test_a_line_must_name_exactly_the_snapshot_fields(
    line: dict[str, object],
) -> None:
    """Both a missing and an extra field make the line unusable."""
    with pytest.raises(ValueError, match="exactly the snapshot fields"):
        event_from_dict(line)


def test_identity_must_be_a_64_character_digest() -> None:
    """A short identity would be a clear-text key, so it is refused."""
    with pytest.raises(TypeError, match="64-character digest"):
        event_from_dict(_line(identity="crate-name"))


@pytest.mark.parametrize(
    ("origin", "label"),
    [("cargo", "hit"), ("registry", "maybe"), ("", "hit")],
)
def test_the_origin_and_label_vocabulary_is_closed(origin: str, label: str) -> None:
    """Only the recorded origins and the three labels are accepted."""
    with pytest.raises(ValueError, match="accepted vocabulary"):
        event_from_dict(_line(origin=origin, label=label))


@pytest.mark.parametrize(
    ("runtime", "error", "match"),
    [
        pytest.param("0.25", TypeError, "runtime_seconds", id="text"),
        pytest.param(True, TypeError, "runtime_seconds", id="is-a-bool"),
        pytest.param(None, TypeError, "runtime_seconds", id="null"),
        pytest.param(float("nan"), ValueError, "must be finite", id="nan"),
        pytest.param(float("inf"), ValueError, "must be finite", id="infinity"),
        pytest.param(
            float("-inf"), ValueError, "must be finite", id="negative-infinity"
        ),
    ],
)
def test_runtime_must_be_a_finite_number(
    runtime: object, error: type[Exception], match: str
) -> None:
    """A non-number is a type error; a non-finite number is a value error."""
    with pytest.raises(error, match=match):
        event_from_dict(_line(runtime_seconds=runtime))


@pytest.mark.parametrize(
    "stamp",
    [
        pytest.param("2026-01-01T00:00:00", id="missing-offset"),
        pytest.param("not-a-time", id="unparsable"),
    ],
)
def test_the_label_time_must_be_an_offset_timestamp(stamp: str) -> None:
    """UTC text without an offset is ambiguous and is refused."""
    with pytest.raises(ValueError, match="timestamp"):
        event_from_dict(_line(observed_at=stamp))


def test_an_admission_time_must_be_text_or_null() -> None:
    """A numeric admission time is not a timestamp."""
    with pytest.raises(TypeError, match="text or null"):
        event_from_dict(_line(admitted_at=7))


def test_an_absent_admission_time_stays_absent() -> None:
    """A null admission time decodes to None rather than the epoch."""
    event = event_from_dict(_line(admitted_at=None))

    assert event.admitted_at is None, "no admission time is invented"


def test_a_missing_label_time_is_refused() -> None:
    """Every event has a label time; null is not a placeholder for one."""
    with pytest.raises(ValueError, match="observed_at must be a UTC timestamp"):
        event_from_dict(_line(observed_at=None))


def _causal(seconds: int, label: str) -> Event:
    """Return one event for identity ``a`` admitted five seconds before its label."""
    return Event(
        "a" * 64,
        ORIGIN,
        EPOCH + dt.timedelta(seconds=seconds - 5),
        EPOCH + dt.timedelta(seconds=seconds),
        typ.cast("Label", label),
        1.0,
    )


def _admitted(identity: str, *, admitted: int, observed: int) -> Event:
    """Return one event with explicit admission and label times."""
    return Event(
        identity,
        ORIGIN,
        EPOCH + dt.timedelta(seconds=admitted),
        EPOCH + dt.timedelta(seconds=observed),
        "hit",
        1.0,
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
            _causal(60, "miss"),
            _causal(60, "miss"),
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


def test_batches_follow_store_order_when_a_timestamp_steps_back() -> None:
    """A backward clock step never merges records from separate windows."""
    first = _causal(10, "hit")
    stepped = Event(
        "b" * 64,
        ORIGIN,
        EPOCH,
        EPOCH + dt.timedelta(seconds=5),
        "hit",
        1.0,
    )
    third = _causal(20, "miss")

    batches = label_batches((first, stepped, third))

    assert batches == ((0,), (1,), (2,)), "contiguity beats timestamp equality"
    rows = features((first, stepped, third), load_window_seconds=60.0)
    assert typ.cast("Features", rows[1]).load == 0, "the tied label is not earlier"
    assert typ.cast("Features", rows[2]).load == 2, "both earlier labels are history"


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
    )

    assert admission_moment(event) == expected, (
        "only a recorded time at or before the label"
    )
