"""The archive run's bounded metrics event and the retries it reports."""

import collections.abc as cabc
import datetime as dt
import typing as typ
from pathlib import Path

import pytest
from archive_seal_helpers import seal_five_rows_once
from outcome_archive_helpers import NOW, layout, policy, records, write

from buildlimits import (
    outcome_archive_active,
    outcome_archive_retain,
    outcome_history,
    outcome_history_pin,
)
from buildlimits.metrics import Metrics
from buildlimits.outcome_archive import RunOptions, inspect_store, run_archive
from buildlimits.outcome_archive_active import active_history
from buildlimits.outcome_archive_history import ActiveHistory
from buildlimits.outcome_archive_lock import ArchiveLock
from buildlimits.outcome_archive_retain import settled_snapshot
from buildlimits.outcome_history import read_history
from buildlimits.outcome_history_pin import (
    HISTORY_ATTEMPTS,
    HistoryFaultError,
    pin_history,
)
from buildlimits.outcome_record_format import StoreHeader
from buildlimits.outcome_segments import SegmentLayout
from buildlimits.retry_notice import report_retry

if typ.TYPE_CHECKING:
    from conftest import RecordingSink

    from buildlimits.outcome_archive_report import RunReport
    from buildlimits.outcome_segment_inventory import Inventory

_FINISHED = NOW + dt.timedelta(seconds=2)


class _RetryLog:
    """Collect every retry notice an operation reports."""

    def __init__(self) -> None:
        """Start with no notices."""
        self.notices: list[tuple[str, int, str]] = []

    def __call__(self, operation: str, attempt: int, category: str) -> None:
        """Record one notice."""
        self.notices.append((operation, attempt, category))


def _run(tmp_path: Path, port: Metrics) -> RunReport:
    """Run one firing at a fixed start and finish."""
    return run_archive(
        layout(tmp_path),
        policy(),
        RunOptions(now=NOW, clock=lambda: _FINISHED, metrics=port),
    )


def test_a_run_publishes_one_bounded_archive_event(
    tmp_path: Path, metric_sink: RecordingSink
) -> None:
    """A firing is visible to dashboards without naming a path."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())

    report = _run(tmp_path, Metrics(metric_sink, source="adapter"))

    assert report.status == "ok", "the run archived the sealed source"
    (record,) = metric_sink.records
    assert record["BUILD_LIMITS_EVENT"] == "archive_run", "one event per firing"
    assert record["BUILD_LIMITS_SOURCE"] == "adapter", "the timer is an adapter"
    assert record["BUILD_LIMITS_ARCHIVE_STATUS"] == "ok", "the status is closed"
    assert record["BUILD_LIMITS_ARCHIVE_REASON"] == "none", "no run-level reason"
    assert record["BUILD_LIMITS_ARCHIVED"] == "1", "the archived count is published"
    assert record["BUILD_LIMITS_TRIMMED"] == "0", "absent actions count zero"
    assert record["BUILD_LIMITS_DELETED"] == "0", "absent actions count zero"
    assert record["BUILD_LIMITS_QUARANTINED"] == "0", "absent actions count zero"
    assert record["BUILD_LIMITS_FAILURES"] == "0", "no sequence failed"
    assert record["BUILD_LIMITS_RUNTIME_SECONDS"] == "2.000", "the span is published"
    assert str(tmp_path) not in " ".join(record.values()), "no path is published"


def test_a_busy_firing_publishes_its_retryable_status(
    tmp_path: Path, metric_sink: RecordingSink
) -> None:
    """A lock held by another run is retryable, not a failure."""
    port = Metrics(metric_sink, source="adapter")

    with ArchiveLock(layout(tmp_path).lock_file):
        report = _run(tmp_path, port)

    assert report.status == "busy", "the run did not start"
    (record,) = metric_sink.records
    assert record["BUILD_LIMITS_ARCHIVE_STATUS"] == "busy", "the status is published"
    assert record["BUILD_LIMITS_ARCHIVE_REASON"] == "busy", "the reason is published"


def test_an_active_scan_retry_names_its_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan whose version changed reports the attempt it abandoned."""
    log = _RetryLog()
    path = tmp_path / "cache-outcomes.jsonl"
    path.write_bytes(b"")
    sizes = iter([10, 11, 11, 11])

    def size_of(_source: object, *, fallback: int = 0) -> int:
        """Return the next scripted size, then the caller's fallback."""
        return next(sizes, fallback)

    def measured(_source: object, *, size: int, cutoff: object = None) -> ActiveHistory:
        """Stand in for the row scan with the size it was given."""
        return ActiveHistory(bytes=size)

    monkeypatch.setattr(outcome_archive_active, "_size_of", size_of)
    monkeypatch.setattr(outcome_archive_active, "_measure", measured)

    history = active_history(path, on_retry=log)

    assert history.bytes == 11, "the settled version is the one returned"
    assert log.notices == [("active_scan", 1, "active_changed")], "one notice"


def test_a_snapshot_retry_names_its_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seal between the two inventory scans reports the retried attempt."""
    log = _RetryLog()
    first = typ.cast("Inventory", object())
    second = typ.cast("Inventory", object())
    reads = iter([first, second, second])

    def read_inventory(*_args: object, **_kwargs: object) -> Inventory:
        """Return the next scripted inventory."""
        return next(reads)

    monkeypatch.setattr(
        outcome_archive_retain.outcome_segment_inventory,
        "read_inventory",
        read_inventory,
    )
    monkeypatch.setattr(
        outcome_archive_retain, "active_history", lambda *_a, **_k: ActiveHistory()
    )

    inventory, _active, settled = settled_snapshot(layout(tmp_path), None, on_retry=log)

    assert settled and inventory is second, "the agreeing pair is accepted"
    assert log.notices == [("snapshot", 1, "inventory_changed")], "one notice"


def test_a_history_pin_retry_names_every_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unsettled pin reports each attempt before it is given up on."""
    log = _RetryLog()
    inventory = typ.cast("Inventory", object())
    monkeypatch.setattr(
        outcome_history_pin.outcome_segment_inventory,
        "read_inventory",
        lambda *_a, **_k: inventory,
    )
    monkeypatch.setattr(outcome_history_pin, "_is_empty_store", lambda *_a, **_k: False)
    monkeypatch.setattr(outcome_history_pin, "_pinned_or_retry", lambda *_a, **_k: None)
    store_layout = SegmentLayout.beside(tmp_path / "cache-outcomes.jsonl")

    with pytest.raises(HistoryFaultError):
        pin_history(store_layout, on_retry=log)

    assert log.notices == [
        ("history_pin", attempt, "unsettled")
        for attempt in range(1, HISTORY_ATTEMPTS + 1)
    ], "every attempt is named"


def _seal_at_first_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SegmentLayout, _RetryLog]:
    """Seal five rows when the first active read runs, collecting notices."""
    write(tmp_path / "cache-outcomes.jsonl", records(NOW, 1), policy())
    store_layout = layout(tmp_path)
    seal_five_rows_once(store_layout, monkeypatch)
    return store_layout, _RetryLog()


@pytest.mark.parametrize("entry", ["run", "inspection"])
def test_the_entry_point_propagates_the_snapshot_retry_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    """A run or inspection reports the snapshot retry its plan made."""
    store_layout, log = _seal_at_first_read(tmp_path, monkeypatch)
    options = RunOptions(now=NOW, clock=lambda: _FINISHED, on_retry=log)

    report = (
        run_archive(store_layout, policy(), options)
        if entry == "run"
        else inspect_store(store_layout, policy(), options=options)
    )

    assert report.status == "ok", "the reading settles after the retry"
    assert ("snapshot", 1, "inventory_changed") in log.notices, (
        "the entry point reports the retry it made"
    )


class _FlakyStream:
    """Refuse one pass as unsettled, then yield an empty history."""

    def __init__(self) -> None:
        """Start at the first pass with one header."""
        self.passes = 0
        self.salt_ids: tuple[str, ...] = ()
        self.header = StoreHeader("salt", NOW)

    def __iter__(self) -> cabc.Iterator[object]:
        """Raise on the first pass and finish on the next."""
        self.passes += 1
        if self.passes == 1:
            raise HistoryFaultError("unsettled")
        return iter(())


def test_a_history_read_reports_an_unsettled_stream_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pinned stream that moves between passes reports its retry."""
    log = _RetryLog()
    stream = _FlakyStream()
    monkeypatch.setattr(outcome_history, "open_history", lambda *_a, **_k: stream)

    decoded = read_history(
        SegmentLayout.beside(tmp_path / "cache-outcomes.jsonl"), on_retry=log
    )

    assert not decoded.records, "the second pass is the one returned"
    assert log.notices == [("history", 1, "unsettled")], "one notice per retry"


def test_the_retry_notice_is_one_identifier_free_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The line an operator greps carries no path and no exception text."""
    report_retry("snapshot", 2, "inventory_changed")

    expected = "retry: snapshot attempt 2 reason inventory_changed\n"
    assert capsys.readouterr().err == expected, "the notice has one closed shape"
