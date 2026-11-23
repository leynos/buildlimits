"""Journal provenance as recorded by the dataset command."""

import typing as typ
from pathlib import Path

import pytest
from outcome_test_helpers import OBSERVED_AT
from predict_test_helpers import write_store

from buildlimits.metrics_query import MetricsQueryError, Window
from buildlimits.metrics_records import Record
from buildlimits.predict import dataset_cli
from buildlimits.predict.journal_provenance import (
    JournalCounts,
    JournalProvenance,
    JournalUnavailable,
    count_journal,
)
from buildlimits.predict.snapshot_store import load_snapshot

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _RaisingJournal:
    """A journal query that fails the way an unavailable journal does."""

    def __init__(self, error: type[Exception] = RuntimeError) -> None:
        """Fail with ``error``, defaulting to an unexpected failure."""
        self._error = error

    def __call__(self, _since: str) -> Window:
        """Report the journal as unreachable."""
        raise self._error("journal unavailable")


def _journal_snapshot(
    tmp_path: Path, journal: cabc.Callable[[str], Window]
) -> tuple[int, JournalProvenance | None]:
    """Snapshot eight rows using ``journal`` and return its status and note."""
    store = tmp_path / "cache-outcomes.jsonl"
    output = tmp_path / "history.jsonl"
    write_store(store, 8)

    status = dataset_cli.main(
        ["--store", str(store), "--output", str(output)],
        edges=dataset_cli.Edges(journal=journal),
    )
    return status, load_snapshot(output).journal


@pytest.mark.parametrize(
    "journal",
    [
        pytest.param(_RaisingJournal(), id="unexpected-error"),
        pytest.param(_RaisingJournal(MetricsQueryError), id="reader-error"),
    ],
)
def test_dataset_command_survives_a_journal_that_fails(
    tmp_path: Path, journal: cabc.Callable[[str], Window]
) -> None:
    """Journal provenance is context, so a journal failure cannot lose history.

    The reader reports an unreadable journal with its own error type rather
    than returning a note, so the command boundary must turn that into the
    provenance note as well as an unexpected failure.
    """
    status, note = _journal_snapshot(tmp_path, journal)

    assert status == 0, "the snapshot is still written"
    assert isinstance(note, JournalUnavailable), "the failure is recorded"
    assert note.reason == "journal query failed", "and named as provenance"


def test_dataset_command_records_journal_counts(tmp_path: Path) -> None:
    """A journal that answers is recorded as provenance beside the snapshot."""
    window = Window(
        records=tuple(
            Record("adapter", "adapter_finished", {}, OBSERVED_AT) for _ in range(12)
        ),
        seconds=86400,
        truncated=False,
    )
    status, note = _journal_snapshot(tmp_path, lambda _since: window)

    assert status == 0, "the snapshot is written"
    assert isinstance(note, JournalCounts), "counts are recorded"
    assert note.records == 12, "every record is kept"
    assert note.by_source == {"adapter": 12}, "and attributed to its source"


def test_observer_counts_survive_the_snapshot_round_trip(tmp_path: Path) -> None:
    """The recorder's counts are written and read back beside the events."""
    window = Window(
        records=(
            Record(
                "supervisor",
                "request_admitted",
                {"cache_path": "cached", "cache_backend": "sccache"},
                OBSERVED_AT,
            ),
            Record(
                "supervisor",
                "cache_outcome",
                {
                    "cache_result": "hit",
                    "outcome_reason": "none",
                    "outcome_attribution": "solo",
                    "crate_origin": "registry",
                },
                OBSERVED_AT,
            ),
        ),
        seconds=3600,
        truncated=False,
    )

    status, note = _journal_snapshot(tmp_path, lambda _since: window)

    assert status == 0, "the snapshot is written"
    assert isinstance(note, JournalCounts), note
    assert note.observer.requests == 1, "the admission survives the file"
    assert note.observer.by_result == {"hit": 1}, "the outcome survives the file"
    assert note.observer.by_cache_path == {"cached": 1}, "the cache path survives"


def test_observer_accounting_keeps_its_denominators_distinct() -> None:
    """Requests, outcomes, readings and store writes stay separate counts."""
    records = (
        Record(
            "supervisor",
            "request_admitted",
            {"cache_path": "cached", "cache_backend": "sccache"},
            OBSERVED_AT,
        ),
        Record(
            "supervisor",
            "request_admitted",
            {"cache_path": "direct", "cache_backend": "sccache"},
            OBSERVED_AT,
        ),
        Record(
            "supervisor",
            "request_admitted",
            {"cache_path": "direct_fallback", "cache_backend": "sccache"},
            OBSERVED_AT,
        ),
        Record(
            "supervisor",
            "cache_outcome",
            {
                "cache_result": "hit",
                "outcome_reason": "none",
                "outcome_attribution": "solo",
                "crate_origin": "registry",
            },
            OBSERVED_AT,
        ),
        Record(
            "supervisor",
            "cache_outcome",
            {
                "cache_result": "miss",
                "outcome_reason": "none",
                "outcome_attribution": "solo",
                "crate_origin": "git",
            },
            OBSERVED_AT,
        ),
        Record(
            "supervisor",
            "cache_outcome",
            {
                "cache_result": "unattributed",
                "outcome_reason": "mixed_window",
                "outcome_attribution": "none",
                "crate_origin": "path",
            },
            OBSERVED_AT,
        ),
        Record(
            "supervisor",
            "outcome_store",
            {
                "store_operation": "append",
                "store_reason": "none",
                "store_records": "10",
                "store_discarded": "2",
            },
            OBSERVED_AT,
        ),
        Record(
            "supervisor",
            "outcome_reading",
            {"readings": "1", "readings_skipped": "5", "readings_failed": "0"},
            OBSERVED_AT,
        ),
        Record(
            "supervisor",
            "outcome_reading",
            {"gate_reason": "queued"},
            OBSERVED_AT,
        ),
        Record(
            "supervisor",
            "outcome_reading",
            {"readings": "1", "readings_skipped": "4", "readings_failed": "1"},
            OBSERVED_AT,
        ),
        Record(
            "supervisor",
            "outcome_reading",
            {"outcome_reason": "timeout"},
            OBSERVED_AT,
        ),
    )

    note = count_journal(records, since="1h", cap=1000, truncated=False)
    observer = note.observer

    assert observer.requests == 3, "every admission is a request"
    assert observer.by_cache_path == {
        "cached": 1,
        "direct": 1,
        "direct_fallback": 1,
    }, "the cache path is named, not collapsed"
    assert observer.outcomes == 3, "outcomes are their own denominator"
    assert observer.by_result == {
        "hit": 1,
        "miss": 1,
        "unattributed": 1,
    }, "results are counted separately"
    assert observer.by_reason == {"mixed_window": 1, "none": 2}, "reasons are named"
    assert observer.by_attribution == {"none": 1, "solo": 2}, "attribution is named"
    assert observer.by_origin == {"git": 1, "path": 1, "registry": 1}, "origins"
    assert observer.store_records == 10, "appended rows are summed"
    assert observer.store_discarded == 2, "dropped or evicted rows are summed"
    assert observer.readings == 2, "completed readings are summed"
    assert observer.readings_skipped == 9, "skipped readings are summed"
    assert observer.readings_failed == 1, "failed readings are summed"
    assert observer.by_gate_reason == {"queued": 1}, "the closed gate is named"
    assert observer.by_reading_reason == {"timeout": 1}, "the reading failure is named"
