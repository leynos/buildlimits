"""Journal provenance as recorded by the dataset command."""

import typing as typ
from pathlib import Path

import pytest
from outcome_test_helpers import OBSERVED_AT
from predict_test_helpers import write_store

from buildlimits.metrics_query import MetricsQueryError, Window
from buildlimits.metrics_records import Record
from buildlimits.predict import dataset_cli, dataset_report
from buildlimits.predict.formatting import TRUNCATED_OBSERVER_NOTE
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
    observer = note.observer
    assert observer is not None, "the counts survive the file"
    assert observer.requests == 1, "the admission survives the file"
    assert observer.by_result == {"hit": 1}, "the outcome survives the file"
    assert observer.by_cache_path == {"cached": 1}, "the cache path survives"


def test_a_failed_reading_is_reported_without_admissions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A window holding only a failed reading still names its failure."""
    note = count_journal(
        (
            Record(
                "supervisor",
                "outcome_reading",
                {"readings_failed": "1", "outcome_reason": "timeout"},
                OBSERVED_AT,
            ),
        ),
        since="4w",
        cap=1000,
        truncated=False,
    )

    dataset_report._report_journal(note)

    output = capsys.readouterr().out
    assert "failed=1" in output, "the reading failure is printed"
    assert "held no observer counts" not in output, "the window is not called empty"


def test_a_gate_only_window_reports_its_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A window whose only record is a gate transition still reports it."""
    note = count_journal(
        (
            Record(
                "supervisor",
                "outcome_reading",
                {"readings": "0", "readings_skipped": "0", "gate_reason": "queued"},
                OBSERVED_AT,
            ),
        ),
        since="4w",
        cap=1000,
        truncated=False,
    )

    dataset_report._report_journal(note)

    output = capsys.readouterr().out
    assert "gate reason: queued=1" in output, "the transition is printed"
    assert "held no observer counts" not in output, "the window is not called empty"


def test_a_truncated_journal_marks_its_observer_counts_partial(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A capped query's observer counts are a lower bound, and say so.

    The same sentence the rendered report prints marks them partial, so a
    reader of either form is told the window is smaller than the lookback.
    """
    note = count_journal(
        (
            Record(
                "supervisor",
                "request_admitted",
                {"cache_path": "cached", "cache_backend": "sccache"},
                OBSERVED_AT,
            ),
        ),
        since="4w",
        cap=1000,
        truncated=True,
    )

    dataset_report._report_journal(note)

    output = capsys.readouterr().out
    assert TRUNCATED_OBSERVER_NOTE in output, "the partial window is named"
    assert "requests=1" in output, "the measured counts are still printed"


def test_a_complete_journal_prints_no_partial_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A window that never reached the cap is printed without the warning."""
    note = count_journal(
        (
            Record(
                "supervisor",
                "request_admitted",
                {"cache_path": "cached", "cache_backend": "sccache"},
                OBSERVED_AT,
            ),
        ),
        since="4w",
        cap=1000,
        truncated=False,
    )

    dataset_report._report_journal(note)

    output = capsys.readouterr().out
    assert "requests=1" in output, "the counts are printed"
    assert TRUNCATED_OBSERVER_NOTE not in output, "a complete window is not marked"


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
                "store_discarded": "0",
            },
            OBSERVED_AT,
        ),
        Record(
            "supervisor",
            "outcome_store",
            {
                "store_operation": "compact",
                "store_reason": "expired",
                "store_records": "0",
                "store_discarded": "7",
            },
            OBSERVED_AT,
        ),
        Record(
            "supervisor",
            "outcome_store",
            {
                "store_operation": "append",
                "store_reason": "save_failed",
                "store_records": "0",
                "store_discarded": "3",
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
    assert observer is not None, "the observer counts are present"

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
    assert observer.store_records == 10, "only appended rows are summed"
    assert observer.store_discarded == 10, "dropped or evicted rows are summed"
    assert observer.by_store_operation == {
        "append": 2,
        "compact": 1,
    }, "each operation is counted once, beside the appends"
    assert observer.by_store_reason == {"expired": 7, "save_failed": 3}, (
        "retention loss and failed writes are told apart"
    )
    assert sum(observer.by_store_reason.values()) == observer.store_discarded, (
        "the breakdown reconciles with the discarded total, and a clean append "
        "does not add a reason of its own"
    )
    assert observer.readings == 2, "completed readings are summed"
    assert observer.readings_skipped == 9, "skipped readings are summed"
    assert observer.readings_failed == 1, "failed readings are summed"
    assert observer.by_gate_reason == {"queued": 1}, "the closed gate is named"
    assert observer.by_reading_reason == {"timeout": 1}, "the reading failure is named"
