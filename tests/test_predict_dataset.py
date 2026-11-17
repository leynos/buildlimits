"""Snapshot extraction, redaction and journal-provenance contracts."""

import dataclasses as dc
import datetime as dt
import typing as typ
from pathlib import Path

import pytest
from outcome_store_helpers import window_summary
from outcome_test_helpers import OBSERVED_AT, attributed_record, cache_key
from predict_test_helpers import write_json

from buildlimits.metrics_query import MetricsQueryError, Window
from buildlimits.metrics_records import Record
from buildlimits.outcome_record_format import DecodedStore, StoreHeader
from buildlimits.predict.dataset import build_snapshot, count_journal, find_gaps
from buildlimits.predict.records import identity_of, label_of
from buildlimits.predict.snapshot_store import load_snapshot, save_snapshot

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_vocabulary import CacheResult

NOW = dt.datetime(2026, 11, 17, 12, tzinfo=dt.UTC)


def _decoded(records: tuple[object, ...]) -> DecodedStore:
    """Wrap records in a store header."""
    return DecodedStore(StoreHeader("fixed-salt", NOW), typ.cast("typ.Any", records))


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("hit", "hit"),
        ("miss", "miss"),
        ("unattributed", "unknown"),
        ("declined", "unknown"),
        ("failed", "unknown"),
        ("error", "unknown"),
    ],
)
def test_only_attributed_outcomes_become_hit_or_miss(
    outcome: CacheResult, expected: str
) -> None:
    """Every non-attributed outcome stays unknown and is never a miss."""
    assert label_of(outcome) == expected, "closed label vocabulary"


def test_snapshot_redacts_identity_and_orders_by_label_time() -> None:
    """The snapshot stores a digest, never the crate name or key fields."""
    later = attributed_record(
        outcome="hit", observed_at=OBSERVED_AT + dt.timedelta(minutes=5)
    )
    earlier = attributed_record(outcome="miss")
    snapshot = build_snapshot(
        _decoded((later, earlier)), now=NOW, gap_threshold_seconds=60.0
    )

    assert [event.label for event in snapshot.events] == ["miss", "hit"], (
        "events ordered by when the label arrived"
    )
    assert snapshot.events[0].identity == identity_of(cache_key()), (
        "identity is the hashed key"
    )
    assert len(snapshot.events[0].identity) == 64, "digest length"
    assert all(event.admitted_at is None for event in snapshot.events), (
        "the store records no admission time"
    )


def test_saved_snapshot_contains_no_clear_text_crate_name(tmp_path: Path) -> None:
    """The written file holds neither the crate name nor the argument digest."""
    snapshot = build_snapshot(_decoded((attributed_record(outcome="hit"),)), now=NOW)
    path = tmp_path / "history.jsonl"

    save_snapshot(path, snapshot)

    document = path.read_text(encoding="utf-8")
    assert "serde" not in document, "crate name redacted"
    assert "a" * 64 not in document, "raw argument digest redacted"


def test_unknown_outcomes_are_counted_but_not_labelled() -> None:
    """Unattributed and declined rows appear in the history without a label."""
    records = (
        attributed_record(outcome="hit"),
        attributed_record(outcome="unattributed"),
        attributed_record(outcome="declined"),
    )
    snapshot = build_snapshot(_decoded(records), now=NOW)
    stats = snapshot.stats()

    assert stats["events"] == 3, "every row is history"
    assert stats["labelled"] == 1, "only the attributed hit is labelled"
    assert stats["labelled_fraction"] == pytest.approx(1 / 3), "labelled share"


def test_window_summaries_never_become_events() -> None:
    """Window summaries carry no compilation identity and are excluded."""
    summary = window_summary(observed_at=OBSERVED_AT)
    snapshot = build_snapshot(
        _decoded((summary, attributed_record(outcome="miss"))), now=NOW
    )

    assert len(snapshot.events) == 1, "summary excluded"
    assert snapshot.source.summary_records == 1, "summary provenance kept"


def test_gaps_report_silences_over_the_threshold() -> None:
    """A silence longer than the threshold is reported with its length."""
    first = attributed_record(outcome="hit")
    second = attributed_record(
        outcome="hit", observed_at=OBSERVED_AT + dt.timedelta(hours=2)
    )
    snapshot = build_snapshot(
        _decoded((first, second)), now=NOW, gap_threshold_seconds=3600.0
    )

    assert len(snapshot.gaps) == 1, "one gap over an hour"
    assert snapshot.gaps[0].seconds == pytest.approx(7200.0), "gap length"


def test_gap_detection_ignores_short_silences() -> None:
    """Ordinary spacing is not reported as a gap."""
    events = (
        attributed_record(outcome="hit"),
        attributed_record(
            outcome="miss", observed_at=OBSERVED_AT + dt.timedelta(seconds=30)
        ),
    )
    snapshot = build_snapshot(_decoded(events), now=NOW)

    assert not find_gaps(snapshot.events, threshold_seconds=60.0), "no gap"


def test_snapshot_round_trip_preserves_events_and_gaps(tmp_path: Path) -> None:
    """Saving and loading returns the same stream and reported gaps."""
    records = (
        attributed_record(outcome="hit"),
        attributed_record(
            outcome="miss", observed_at=OBSERVED_AT + dt.timedelta(hours=3)
        ),
    )
    snapshot = build_snapshot(_decoded(records), now=NOW, gap_threshold_seconds=60.0)
    path = tmp_path / "history.jsonl"

    save_snapshot(path, snapshot)
    loaded = load_snapshot(path)

    assert loaded.events == snapshot.events, "events survive the round trip"
    assert loaded.gaps == snapshot.gaps, "gaps survive the round trip"
    assert loaded.source.salt_id == "fixed-salt", "store provenance survives"


def test_journal_failure_is_reported_not_raised() -> None:
    """An unreadable journal becomes an availability note."""

    def fail(**_options: object) -> Window:
        """Stand in for a journal query the host cannot serve."""
        raise MetricsQueryError("journal unavailable")

    note = count_journal(query=fail)

    assert note["available"] is False, "availability is explicit"
    assert note["reason"] == "journal unavailable", "reason preserved"


def test_journal_counts_name_their_join_limitation() -> None:
    """Journal counts are context only, because they carry no identity."""
    window = Window(
        records=(
            Record("adapter", "adapter_finished", {}, OBSERVED_AT),
            Record("adapter", "adapter_finished", {}, OBSERVED_AT),
            Record("supervisor", "admission", {}, OBSERVED_AT),
        ),
        seconds=3600,
        truncated=False,
    )

    note = count_journal(query=lambda **_options: window)

    assert note["records"] == 3, "all accepted records counted"
    assert note["by_source"] == {"adapter": 2, "supervisor": 1}, "per source"
    assert "no compilation identity" in typ.cast("str", note["join"]), (
        "join limitation stated"
    )


def test_source_provenance_records_the_store_header() -> None:
    """The snapshot keeps the salt identifier and write time it read."""
    snapshot = build_snapshot(_decoded((attributed_record(outcome="hit"),)), now=NOW)
    source = snapshot.source

    assert dc.asdict(source)["salt_id"] == "fixed-salt", "salt retained"
    assert source.written_at == NOW, "write time retained"
    assert source.records_total == 1, "row count retained"


def _document() -> dict[str, object]:
    """Return the encoded snapshot of one attributed hit."""
    snapshot = build_snapshot(_decoded((attributed_record(outcome="hit"),)), now=NOW)
    return snapshot.to_dict()


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda doc: doc.update(snapshot_version=99), id="unknown-version"),
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
