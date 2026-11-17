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

from buildlimits.metrics_records import Record
from buildlimits.outcome_record_format import DecodedStore, StoreHeader
from buildlimits.predict.dataset import StoreHistory, build_snapshot
from buildlimits.predict.events import EPOCH, Event
from buildlimits.predict.journal_provenance import (
    JournalCounts,
    JournalProvenance,
    JournalUnavailable,
    count_journal,
)
from buildlimits.predict.records import decode_history
from buildlimits.predict.snapshot_codec import event_to_dict, snapshot_to_dict
from buildlimits.predict.snapshot_store import load_snapshot

NOW = dt.datetime(2026, 11, 17, 12, tzinfo=dt.UTC)


def _history(*records: object) -> StoreHistory:
    """Decode records into the history value a snapshot is built from."""
    decoded = DecodedStore(StoreHeader("fixed-salt", NOW), typ.cast("typ.Any", records))
    return decode_history(decoded)


def _counts_note() -> JournalCounts:
    """Return a journal note with one witnessed supervisor start."""
    return count_journal(
        (Record("supervisor", "supervisor_started", {}, NOW),),
        since="4w",
        cap=1000,
        truncated=False,
    )


def _document(journal: JournalProvenance | None = None) -> dict[str, object]:
    """Return the encoded snapshot of one attributed hit.

    The default document carries a journal note so its decoding is exercised;
    a caller that means "no journal was queried" passes an empty note.
    """
    note = _counts_note() if journal is None else journal
    snapshot = build_snapshot(
        _history(attributed_record(outcome="hit")), now=NOW, journal=note
    )
    return snapshot_to_dict(snapshot)


def _journal(document: dict[str, object]) -> dict[str, object]:
    """Return the document's mutable journal block."""
    return typ.cast("dict[str, object]", document["journal"])


def _restarts(document: dict[str, object]) -> dict[str, object]:
    """Return the document's mutable restart block."""
    return typ.cast("dict[str, object]", _journal(document)["restarts"])


def _refuses_creation_time(tmp_path: Path, stamp: str, match: str) -> None:
    """Assert the loader refuses ``stamp`` as the snapshot's creation time."""
    document = _document()
    document["created_at"] = stamp

    with pytest.raises(ValueError, match=match):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


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
            lambda doc: _journal(doc).update(available="yes"),
            id="journal-availability-not-a-bool",
        ),
        pytest.param(
            lambda doc: _journal(doc).update(records="2"),
            id="journal-count-not-an-integer",
        ),
        pytest.param(
            lambda doc: _journal(doc).pop("records"),
            id="journal-count-missing",
        ),
        pytest.param(
            lambda doc: doc.update(journal={"available": False, "reason": 7}),
            id="journal-reason-not-text",
        ),
        pytest.param(
            lambda doc: _journal(doc).update(records=-1),
            id="journal-count-negative",
        ),
        pytest.param(
            lambda doc: _journal(doc).update(restarts="2"),
            id="journal-restarts-not-an-object",
        ),
        pytest.param(
            lambda doc: _restarts(doc).update(count="2"),
            id="journal-restarts-count-not-an-integer",
        ),
        pytest.param(
            lambda doc: _restarts(doc).update(count=-1),
            id="journal-restarts-count-negative",
        ),
        pytest.param(
            lambda doc: _restarts(doc).update(first=7),
            id="journal-restarts-bound-not-text",
        ),
        pytest.param(
            lambda doc: _restarts(doc).pop("last"),
            id="journal-restarts-range-without-last",
        ),
        pytest.param(
            lambda doc: _journal(doc).update(span_start="2026-01-01T00:00:00"),
            id="journal-span-start-without-an-offset",
        ),
        pytest.param(
            lambda doc: _journal(doc).update(by_source={"adapter": True}),
            id="journal-tally-count-is-a-bool",
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


@pytest.mark.parametrize("seconds", [float("nan"), float("inf"), 10**400])
def test_a_non_finite_gap_length_is_refused_by_field(
    tmp_path: Path, seconds: float | int
) -> None:
    """A length no finite float can hold is refused, and the gap field is named.

    A JSON integer may be far larger than a float, so the conversion itself can
    overflow rather than produce an infinity.
    """
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
    """The note the report renders is decoded back into its values."""
    loaded = load_snapshot(write_json(tmp_path / "history.jsonl", _document()))

    assert isinstance(loaded.journal, JournalCounts), "counts are decoded"
    assert loaded.journal.available is True, "availability is read"
    assert loaded.journal.records == 1, "the count is read"
    assert loaded.journal.span == "4w", "the requested lookback is read"
    assert loaded.journal.restarts.count == 1, "the restart is read"
    assert loaded.journal.restarts.first == NOW, "and its time"


def test_an_unavailable_journal_note_survives_loading(tmp_path: Path) -> None:
    """A note that names its reason decodes without invented counts."""
    document = _document(JournalUnavailable("journal query skipped"))

    loaded = load_snapshot(write_json(tmp_path / "history.jsonl", document))

    assert isinstance(loaded.journal, JournalUnavailable), "the reason is kept"
    assert loaded.journal.reason == "journal query skipped", "and read verbatim"


def test_an_empty_journal_window_decodes_as_counts(tmp_path: Path) -> None:
    """An empty window is counts with no span, not an unavailable journal."""
    empty = count_journal((), since="4w", cap=1000, truncated=False)
    document = _document(empty)

    loaded = load_snapshot(write_json(tmp_path / "history.jsonl", document))

    assert isinstance(loaded.journal, JournalCounts), "counts are decoded"
    assert loaded.journal.available is False, "the window held nothing"
    assert loaded.journal.span_start is None, "and claims no span"


def test_a_snapshot_that_queried_no_journal_decodes_no_note(tmp_path: Path) -> None:
    """An empty block means the snapshot target queried no journal."""
    document = _document(JournalUnavailable("unused"))
    document["journal"] = {}

    loaded = load_snapshot(write_json(tmp_path / "history.jsonl", document))

    assert loaded.journal is None, "no note is invented"


def _event(identity_byte: str, minutes: int, *, admitted: bool) -> dict[str, object]:
    """Return one snapshot event at a fixed offset from the epoch.

    Returns
    -------
    dict[str, object]
        The event's encoded shape.
    """
    event = Event(
        identity_byte * 64,
        "git",
        EPOCH if admitted else None,
        EPOCH + dt.timedelta(minutes=minutes),
        "hit",
        1.0,
    )
    return event_to_dict(event)


def _backward_history(*, admitted: bool) -> dict[str, object]:
    """Return a snapshot whose second event was observed before the first.

    Returns
    -------
    dict[str, object]
        A two-event document, with admission times when ``admitted`` is set.
    """
    document = _document()
    document["events"] = [
        _event("b", 10, admitted=admitted),
        _event("c", 5, admitted=admitted),
    ]
    return document


def test_an_admission_bearing_history_with_a_backward_clock_is_refused(
    tmp_path: Path,
) -> None:
    """A clock that ran backwards cannot order predictions by recorded time.

    The later store position carries the earlier stamp, so across the step a
    label observed after a prediction could sort before it. No monotonic
    sequence is stored to separate the two sides, so the history is refused.
    """
    document = _backward_history(admitted=True)

    with pytest.raises(ValueError, match="ran backwards"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_a_history_without_admission_times_may_report_a_backward_clock(
    tmp_path: Path,
) -> None:
    """The step only matters where admission times would order predictions."""
    document = _backward_history(admitted=False)

    loaded = load_snapshot(write_json(tmp_path / "history.jsonl", document))

    assert len(loaded.events) == 2, "the history is still readable"
    assert all(event.admitted_at is None for event in loaded.events), (
        "no admission time can be ordered by the broken clock"
    )


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


@pytest.mark.parametrize(
    ("stamp", "match"),
    [
        pytest.param("2026-11-17T12:00:00", "offset", id="without-an-offset"),
        # The earliest date plus a positive offset has no UTC equivalent:
        # converting it needs year zero, which a datetime cannot hold.
        pytest.param("0001-01-01T00:00:00+01:00", "convert to UTC", id="beyond-utc"),
    ],
)
def test_a_creation_time_utc_cannot_read_is_refused(
    tmp_path: Path, stamp: str, match: str
) -> None:
    """A local time is ambiguous, and a positive offset can leave the range.

    The loader names the field in both cases instead of letting the ambiguity
    or an ``OverflowError`` escape as a traceback.
    """
    _refuses_creation_time(tmp_path, stamp, match)


def test_a_representable_offset_timestamp_is_read(tmp_path: Path) -> None:
    """An offset that stays inside the range is converted, not refused."""
    document = _document()
    document["created_at"] = "0001-01-01T01:00:00+01:00"

    loaded = load_snapshot(write_json(tmp_path / "history.jsonl", document))

    assert loaded.created_at == dt.datetime(1, 1, 1, tzinfo=dt.UTC), (
        "the same instant in UTC"
    )
