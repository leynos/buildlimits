"""Snapshot-line validation contracts for the event stream."""

import datetime as dt
import typing as typ

import pytest

from buildlimits.predict.events import EPOCH, Event, event_from_dict, ordered

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import Origin

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


@pytest.mark.parametrize("runtime", ["0.25", True, None])
def test_runtime_must_be_numeric(runtime: object) -> None:
    """A boolean or a string is not a duration."""
    with pytest.raises(TypeError, match="runtime_seconds"):
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


def test_ordering_keeps_stream_order_for_equal_times() -> None:
    """Events sharing a label time keep the order they were read in."""
    first = Event("a" * 64, ORIGIN, None, EPOCH, "hit", 0.1)
    second = Event("b" * 64, ORIGIN, None, EPOCH, "miss", 0.2)

    assert ordered((first, second)) == (first, second), "stable order"
    assert ordered((second, first)) == (second, first), "stable order reversed"


def test_ordering_follows_label_time_not_input_order() -> None:
    """An event whose label arrived earlier comes first."""
    later = Event("a" * 64, ORIGIN, None, EPOCH + dt.timedelta(minutes=1), "hit", 0.1)
    earlier = Event("b" * 64, ORIGIN, None, EPOCH, "miss", 0.2)

    assert ordered((later, earlier)) == (earlier, later), "label order wins"
