"""Version-two outcome rows carry admission and classification evidence."""

import dataclasses as dc
import datetime as dt
import json
import typing as typ

import pytest
from outcome_test_helpers import OBSERVED_AT, attributed_record, cache_key

from buildlimits.outcome_record_codec import decode_lines
from buildlimits.outcome_record_encode import encode_line
from buildlimits.outcome_record_format import (
    DecodedStore,
    OutcomeRecord,
    RecordFailure,
    StoreHeader,
)

ADMITTED_AT = OBSERVED_AT - dt.timedelta(seconds=2)
_SALT = "fixed-salt"


def _header(version: int = 1) -> str:
    """Return one store header line for the requested format version."""
    return json.dumps(
        {
            "format": "buildlimits-cache-outcomes",
            "version": version,
            "salt_id": _SALT,
            "written_at": "2026-10-24T12:00:00Z",
        },
        separators=(",", ":"),
    )


def _v1_row(**changes: object) -> str:
    """Return one version-one outcome row without the new evidence fields."""
    row: dict[str, object] = {
        "kind": "outcome",
        "key": dc.asdict(cache_key()),
        "outcome": "hit",
        "origin": "registry",
        "runtime_seconds": 0.25,
        "observed_at": "2026-10-24T12:00:00Z",
        "parser_class": "cacheable",
        "child_sighting": "unavailable",
        "child_samples": 0,
    }
    row.update(changes)
    return json.dumps(row, separators=(",", ":"), sort_keys=True)


def _v2_row() -> str:
    """Return one version-two row carrying every optional field."""
    return encode_line(
        dc.replace(
            attributed_record(outcome="miss"),
            outcome_reason="mixed_window",
            outcome_attribution="none",
            admitted_at=ADMITTED_AT,
        )
    )


def _decode(*lines: str) -> DecodedStore | RecordFailure:
    """Decode the supplied JSON Lines with the real store codec."""
    return decode_lines("\n".join(lines) + "\n")


def _records(loaded: DecodedStore | RecordFailure) -> tuple[object, ...]:
    """Require a successful decode before a test reads its rows.

    Returns
    -------
    tuple[object, ...]
        The decoded rows. The assertion names a refused document.
    """
    assert isinstance(loaded, DecodedStore), loaded
    return loaded.records


def test_a_version_two_row_round_trips_its_evidence() -> None:
    """Admission time, reason and attribution survive encoding exactly."""
    record = dc.replace(
        attributed_record(outcome="hit"),
        outcome_reason="none",
        outcome_attribution="solo",
        admitted_at=ADMITTED_AT,
    )
    header = StoreHeader(_SALT, OBSERVED_AT)
    document = "\n".join((encode_line(header), encode_line(record))) + "\n"

    loaded = decode_lines(document)

    assert isinstance(loaded, DecodedStore), loaded
    assert loaded.records == (record,), "every field survives the round trip"
    assert isinstance(loaded.records[0], OutcomeRecord), loaded.records[0]
    assert loaded.records[0].admitted_at == ADMITTED_AT, "admission is preserved"
    assert loaded.records[0].outcome_reason == "none", "reason is preserved"
    assert loaded.records[0].outcome_attribution == "solo", "attribution is preserved"


def test_a_version_one_row_decodes_without_guessed_evidence() -> None:
    """An old row stays readable and gains no admission time by inference."""
    loaded = _decode(_header(), _v1_row())

    assert isinstance(loaded, DecodedStore), loaded
    (record,) = typ.cast("tuple[typ.Any, ...]", loaded.records)
    assert record.outcome == "hit", "the old result is preserved"
    assert record.outcome_reason is None, "no reason is invented"
    assert record.outcome_attribution is None, "no attribution is invented"
    assert record.admitted_at is None, "no admission time is invented"


def test_a_version_one_row_cannot_become_scorable() -> None:
    """A decoded old row produces a benchmark event with no admission time."""
    from buildlimits.predict.events import admission_moment
    from buildlimits.predict.records import event_of

    loaded = _decode(_header(), _v1_row())
    (record,) = typ.cast("tuple[typ.Any, ...]", _records(loaded))
    event = event_of(record)

    assert event.admitted_at is None, "the event carries no admission time"
    assert admission_moment(event) is None, "so it cannot be predicted"


@pytest.mark.parametrize(
    "admitted",
    [
        "not-a-time",
        17,
        "2026-10-24T12:00:00+02:00",
        "2026-10-24T12:00:00",
    ],
    ids=["not-text", "not-a-string", "non-utc", "naive"],
)
def test_a_malformed_admission_time_is_corruption(admitted: object) -> None:
    """A present but unusable admission time is refused, never read as absent."""
    row = json.loads(_v2_row())
    row["admitted_at"] = admitted

    loaded = _decode(_header(), json.dumps(row, separators=(",", ":")))

    assert isinstance(loaded, RecordFailure), loaded
    assert loaded.reason == "corrupt", "a malformed time is not a missing time"


def test_a_version_two_row_missing_an_evidence_field_is_corrupt() -> None:
    """The version-two shape is exact, so a dropped field is not accepted."""
    row = json.loads(_v2_row())
    row.pop("outcome_reason")

    loaded = _decode(_header(), json.dumps(row, separators=(",", ":")))

    assert isinstance(loaded, RecordFailure), loaded
    assert loaded.reason == "corrupt", "the row shape is validated exactly"


def test_the_encoder_refuses_a_naive_admission_time() -> None:
    """A naive time has no offset to read, so it cannot be persisted."""
    record = dc.replace(
        attributed_record(outcome="hit"),
        admitted_at=OBSERVED_AT.replace(tzinfo=None),
    )

    with pytest.raises(ValueError, match="invalid"):
        encode_line(record)


def test_an_explicit_null_admission_remains_unknown() -> None:
    """A row written without admission keeps the explicit unknown value."""
    row = json.loads(_v2_row())
    row["admitted_at"] = None

    loaded = _decode(_header(), json.dumps(row, separators=(",", ":")))
    (record,) = typ.cast("tuple[typ.Any, ...]", _records(loaded))

    assert record.admitted_at is None, "null decodes as unknown"
