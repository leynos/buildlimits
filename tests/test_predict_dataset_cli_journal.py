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
