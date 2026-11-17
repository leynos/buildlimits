"""Snapshot encoding: the loader refuses every malformed document.

The extraction tests cover what a snapshot contains; these cover what the
loader will accept back, so a hand-edited file cannot smuggle a broken gap,
journal note or timestamp into a run.
"""

import collections.abc as cabc
import datetime as dt
import typing as typ
from pathlib import Path

import pytest
from outcome_test_helpers import attributed_record
from predict_test_helpers import write_json

from buildlimits.outcome_record_format import DecodedStore, StoreHeader
from buildlimits.predict.dataset import build_snapshot
from buildlimits.predict.snapshot_store import load_snapshot

NOW = dt.datetime(2026, 11, 17, 12, tzinfo=dt.UTC)


def _document() -> dict[str, object]:
    """Return the encoded snapshot of one attributed hit."""
    records = (attributed_record(outcome="hit"),)
    decoded = DecodedStore(StoreHeader("fixed-salt", NOW), typ.cast("typ.Any", records))
    return build_snapshot(decoded, now=NOW).to_dict()


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda doc: doc.update(snapshot_version=99), id="unknown-version"),
        pytest.param(
            lambda doc: doc.update(snapshot_version=True), id="version-is-a-bool"
        ),
        pytest.param(
            lambda doc: doc.update(snapshot_version=1.0), id="version-is-a-float"
        ),
        pytest.param(lambda doc: doc.update(events={}), id="events-not-a-list"),
        pytest.param(lambda doc: doc.update(source=[]), id="source-not-an-object"),
        pytest.param(lambda doc: doc.update(journal=[]), id="journal-not-an-object"),
        pytest.param(lambda doc: doc.update(created_at=7), id="created-at-not-text"),
        pytest.param(
            lambda doc: typ.cast("dict[str, object]", doc["source"]).update(salt_id=7),
            id="salt-not-text",
        ),
        pytest.param(
            lambda doc: typ.cast("dict[str, object]", doc["source"]).update(
                records_total=True
            ),
            id="count-is-a-bool",
        ),
        pytest.param(lambda doc: doc.pop("stats"), id="stats-missing"),
        pytest.param(
            lambda doc: typ.cast("dict[str, object]", doc["stats"]).update(gaps={}),
            id="gaps-not-a-list",
        ),
        pytest.param(
            lambda doc: typ.cast("dict[str, object]", doc["stats"]).update(gaps=[[]]),
            id="gap-not-an-object",
        ),
        pytest.param(
            lambda doc: typ.cast("dict[str, object]", doc["stats"]).update(
                gaps=[{"start": 7, "end": "b", "seconds": 1.0}]
            ),
            id="gap-start-not-text",
        ),
        pytest.param(
            lambda doc: typ.cast("dict[str, object]", doc["stats"]).update(
                gaps=[
                    {
                        "start": "2026-11-17T12:00:00+00:00",
                        "end": "2026-11-17T13:00:00+00:00",
                        "seconds": "3600",
                    }
                ]
            ),
            id="gap-seconds-not-numeric",
        ),
        pytest.param(
            lambda doc: doc.update(journal={"available": "yes"}),
            id="journal-availability-not-a-bool",
        ),
        pytest.param(
            lambda doc: doc.update(journal={"available": True, "records": "2"}),
            id="journal-count-not-an-integer",
        ),
        pytest.param(
            lambda doc: doc.update(journal={"available": True}),
            id="journal-count-missing",
        ),
        pytest.param(
            lambda doc: doc.update(journal={"available": False, "reason": 7}),
            id="journal-reason-not-text",
        ),
        pytest.param(
            lambda doc: doc.update(journal={"available": True, "records": -1}),
            id="journal-count-negative",
        ),
    ],
)
def test_a_malformed_snapshot_is_refused(
    tmp_path: Path, mutate: cabc.Callable[[dict[str, object]], object]
) -> None:
    """Every field the loader reads is checked for type and shape."""
    document = _document()
    mutate(document)

    with pytest.raises((TypeError, ValueError)):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


@pytest.mark.parametrize("seconds", [float("nan"), float("inf")])
def test_a_non_finite_gap_length_is_refused_by_field(
    tmp_path: Path, seconds: float
) -> None:
    """A non-finite length is not a JSON number, so the gap field is named."""
    document = _document()
    stats = typ.cast("dict[str, object]", document["stats"])
    stats.update(
        gaps=[
            {
                "start": "2026-11-17T12:00:00+00:00",
                "end": "2026-11-17T13:00:00+00:00",
                "seconds": seconds,
            }
        ]
    )

    with pytest.raises(ValueError, match="gap must hold finite seconds"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_an_available_journal_note_survives_loading(tmp_path: Path) -> None:
    """The note the report renders is decoded unchanged."""
    document = _document()
    document["journal"] = {"available": True, "span": "4w", "records": 12}

    loaded = load_snapshot(write_json(tmp_path / "history.jsonl", document))

    assert loaded.journal["available"] is True, "availability is read"
    assert loaded.journal["records"] == 12, "the count is read"


def test_a_document_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    """A JSON array is not a snapshot."""
    with pytest.raises(TypeError, match="must hold one JSON object"):
        load_snapshot(write_json(tmp_path / "history.jsonl", [_document()]))


def test_a_hand_edited_gap_survives_loading(tmp_path: Path) -> None:
    """A well-formed gap row is decoded back into a Gap."""
    document = _document()
    stats = typ.cast("dict[str, object]", document["stats"])
    stats["gaps"] = [
        {
            "start": "2026-11-17T12:00:00+00:00",
            "end": "2026-11-17T14:30:00+00:00",
            "seconds": 9000.0,
        }
    ]

    loaded = load_snapshot(write_json(tmp_path / "history.jsonl", document))

    assert len(loaded.gaps) == 1, "the gap is read"
    assert loaded.gaps[0].seconds == pytest.approx(9000.0), "length preserved"


def test_a_timestamp_without_an_offset_is_refused(tmp_path: Path) -> None:
    """A local time would be ambiguous, so the snapshot must carry an offset."""
    document = _document()
    document["created_at"] = "2026-11-17T12:00:00"

    with pytest.raises(ValueError, match="offset"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))
