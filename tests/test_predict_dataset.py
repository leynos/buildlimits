"""Snapshot extraction, redaction and journal-provenance contracts."""

import dataclasses as dc
import datetime as dt
import typing as typ
from pathlib import Path

import pytest
from outcome_store_helpers import window_summary
from outcome_test_helpers import OBSERVED_AT, attributed_record, cache_key

from buildlimits.metrics_query import MetricsQueryError, Window
from buildlimits.metrics_records import Record
from buildlimits.outcome_record_format import DecodedStore, StoreHeader
from buildlimits.predict.dataset import build_snapshot, count_journal, find_gaps
from buildlimits.predict.records import identity_of, label_of
from buildlimits.predict.snapshot_store import load_snapshot, save_snapshot
from buildlimits.utc_text import utc_text

if typ.TYPE_CHECKING:
    from buildlimits.outcome_vocabulary import CacheResult
    from buildlimits.predict.dataset import Snapshot

NOW = dt.datetime(2026, 11, 17, 12, tzinfo=dt.UTC)


def _decoded(records: tuple[object, ...]) -> DecodedStore:
    """Wrap records in a store header."""
    return DecodedStore(StoreHeader("fixed-salt", NOW), typ.cast("typ.Any", records))


def _one_hit_snapshot() -> Snapshot:
    """Return a snapshot holding one attributed hit."""
    return build_snapshot(_decoded((attributed_record(outcome="hit"),)), now=NOW)


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


def test_snapshot_redacts_identity_and_keeps_store_order() -> None:
    """The snapshot stores a digest and keeps the order the store appended."""
    later_stamp = attributed_record(
        outcome="hit", observed_at=OBSERVED_AT + dt.timedelta(minutes=5)
    )
    earlier_stamp = attributed_record(outcome="miss")
    snapshot = build_snapshot(
        _decoded((later_stamp, earlier_stamp)), now=NOW, gap_threshold_seconds=60.0
    )

    assert [event.label for event in snapshot.events] == ["hit", "miss"], (
        "the store's order is never replaced by a timestamp sort"
    )
    assert snapshot.events[0].identity == identity_of(cache_key()), (
        "identity is the hashed key"
    )
    assert len(snapshot.events[0].identity) == 64, "digest length"
    assert all(event.admitted_at is None for event in snapshot.events), (
        "the store records no admission time"
    )
    stats = snapshot.stats()
    assert stats["admission_recorded"] == 0, "nothing was recorded"
    assert stats["admission_missing"] == 2, "so both rows are counted as missing"
    assert stats["admission_usable"] == 0, "and neither can seed a prediction"
    assert stats["admission_unusable"] == 0, "no recorded time is merely unusable"


def test_saved_snapshot_contains_no_clear_text_crate_name(tmp_path: Path) -> None:
    """The written file holds neither the crate name nor the argument digest."""
    snapshot = _one_hit_snapshot()
    path = tmp_path / "history.jsonl"

    save_snapshot(path, snapshot)

    document = path.read_text(encoding="utf-8")
    assert "serde" not in document, "crate name redacted"
    assert "a" * 64 not in document, "raw argument digest redacted"


def test_a_failed_snapshot_write_leaves_the_earlier_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that fails must not truncate the snapshot it would replace."""
    path = tmp_path / "history.jsonl"
    path.write_text("earlier history\n", encoding="utf-8")
    snapshot = _one_hit_snapshot()

    def refuse(_temporary: Path, _target: Path) -> None:
        """Stand in for a replace that fails once the temporary file is written."""
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", refuse)

    with pytest.raises(OSError, match="disk full"):
        save_snapshot(path, snapshot)

    assert path.read_text(encoding="utf-8") == "earlier history\n", "kept intact"
    assert list(tmp_path.iterdir()) == [path], "no temporary file is left behind"


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
    assert note["span_start"] == note["span_end"] == utc_text(OBSERVED_AT), (
        "the retention span the journal really covers"
    )
    assert "no joinable per-compilation admission record" in typ.cast(
        "str", note["correlation"]
    ), "the join limit is stated, not guessed around"


def test_journal_supervisor_starts_are_reported_as_restarts() -> None:
    """A restart the journal witnessed is reported by its first and last time."""
    first = OBSERVED_AT
    last = OBSERVED_AT + dt.timedelta(hours=6)
    window = Window(
        records=(
            Record("supervisor", "supervisor_started", {}, first),
            Record("adapter", "adapter_finished", {}, first),
            Record("supervisor", "supervisor_started", {}, last),
        ),
        seconds=86400,
        truncated=False,
    )

    note = count_journal(query=lambda **_options: window)

    restarts = typ.cast("dict[str, object]", note["restarts"])
    assert restarts["count"] == 2, "both starts are counted"
    assert restarts["first"] == utc_text(first), "the earliest start is named"
    assert restarts["last"] == utc_text(last), "and the latest"


def test_a_truncated_journal_query_says_the_cap_was_reached() -> None:
    """A cap reached means older records are outside what the query saw."""
    window = Window(
        records=(Record("adapter", "adapter_finished", {}, OBSERVED_AT),),
        seconds=60,
        truncated=True,
    )

    note = count_journal(since="24h", query=lambda **_options: window)

    assert "requested 24h lookback" in typ.cast("str", note["retention"]), (
        "the requested span is named"
    )
    assert "the cap was reached" in typ.cast("str", note["retention"]), (
        "and the truncation is admitted"
    )


def test_an_empty_journal_window_reports_no_span() -> None:
    """A query that saw nothing must not present an invented retention span."""
    note = count_journal(
        query=lambda **_options: Window(records=(), seconds=0, truncated=False)
    )

    assert note["available"] is False, "nothing is available"
    assert note["span_start"] is None and note["span_end"] is None, "no span"


def test_source_provenance_records_the_store_header() -> None:
    """The snapshot keeps the salt identifier and write time it read."""
    snapshot = build_snapshot(_decoded((attributed_record(outcome="hit"),)), now=NOW)
    source = snapshot.source

    assert dc.asdict(source)["salt_id"] == "fixed-salt", "salt retained"
    assert source.written_at == NOW, "write time retained"
    assert source.records_total == 1, "row count retained"
