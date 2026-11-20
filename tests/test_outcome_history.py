"""Archive-aware history reading.

History spans archived XZ segments, sealed plain segments and the writer's
active file. These tests build a real segmented store with the writer, archive
it with the archiver, and then read it back through the history view.
"""

import datetime as dt
import tracemalloc
import typing as typ
from pathlib import Path

import pytest
from history_fixtures import (
    NOW,
    SALT_NAME,
    active_records,
    archived_segments,
    observed,
    records,
    window,
)
from outcome_archive_helpers import write as write_store
from segment_test_helpers import history_policy

from buildlimits import outcome_history, outcome_record_encode, outcome_replay_cli
from buildlimits.outcome_archive import RunOptions, run_archive
from buildlimits.outcome_segments import SegmentLayout, archive_name
from buildlimits.outcome_store_io import read_store

# The first two rows of every two-record fixture, so a fault test states only
# the reason its own injection is expected to produce.
_TWO_ROWS = (NOW, NOW - dt.timedelta(minutes=1))

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import DecodedStore


def test_a_generated_history_archives_and_replays_in_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A multi-segment generated history survives the whole archive-replay path.

    The archive pass is traced so the bounded-memory claim is measured on the
    full path rather than inferred from one segment's benchmark.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    rows = records(0, 6_000)
    policy = history_policy(512 * 1024)
    write_store(store, rows, policy)
    layout = SegmentLayout.beside(store)
    source_bytes = sum(
        path.stat().st_size for path in layout.sealed_dir.glob("*.jsonl")
    )
    assert source_bytes > 3_000_000, "the fixture is large enough to matter"

    tracemalloc.start()
    try:
        report = run_archive(layout, policy, RunOptions(now=NOW))
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert report.status == "ok", "the generated history archives cleanly"
    assert len(list(layout.archive_dir.glob("*.jsonl.xz"))) >= 4, (
        "the history spans several segments"
    )
    # The bound is the compressor's own footprint, not the history's: liblzma
    # allocates through Python's allocator, so the traced peak is dominated by
    # the preset's encoder dictionary. It must not grow with the rows read.
    assert peak < 128 * 1024 * 1024, (
        f"archiving {source_bytes} bytes peaked at {peak} traced bytes"
    )

    tracemalloc.start()
    history = outcome_history.read_history_beside(store)
    _current, material_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert observed(history.records) == tuple(
        NOW - dt.timedelta(minutes=index) for index in range(6_000)
    ), "every row is read in append order"

    status = outcome_replay_cli.main([
        "h2",
        "--store",
        str(store),
        "--history",
        "--json",
    ])

    assert status == 0, "the archived history replays"
    assert '"attributed_outcomes": 6000' in capsys.readouterr().out, (
        "replay sees every archived row"
    )

    tracemalloc.start()
    try:
        streamed = outcome_replay_cli.main([
            "h3",
            "--store",
            str(store),
            "--history",
            "--json",
        ])
        _current, replay_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert streamed == 0, "the streaming replay reads the archived history"
    assert '"attributed_outcomes": 6000' in capsys.readouterr().out, (
        "the streaming replay judges every row"
    )
    # H3 holds one segment's decoder state and one running tally, so it must
    # cost less than the materializing read above, whose peak also covers six
    # thousand decoded records. Both peaks include liblzma's decoder
    # dictionary, which is the floor for either path.
    assert replay_peak < material_peak, (
        f"streaming replay peaked at {replay_peak} bytes, not below the "
        f"materializing read's {material_peak}"
    )


def test_history_stream_yields_rows_before_a_later_fault(
    tmp_path: Path,
) -> None:
    """A stream hands out earlier rows before a damaged row is reached.

    A reader that buffered the whole history would raise before returning
    anything, so taking one record first proves the stream decodes as the
    caller reads.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    write_store(store, records(0, 3), history_policy(1))
    layout = SegmentLayout.beside(store)
    damaged = max(layout.sealed_dir.glob("*.jsonl"))
    damaged.write_bytes(damaged.read_bytes() + b"not-json\n")

    stream = outcome_history.open_history_beside(store)
    iterator = iter(stream)

    assert next(iterator).observed_at == NOW, (
        "the oldest row arrives before the damaged segment is read"
    )
    with pytest.raises(outcome_history.HistoryFaultError):
        for _row in iterator:
            pass


def test_history_stream_repeats_the_same_snapshot(tmp_path: Path) -> None:
    """Two passes over one stream read the same rows in the same order."""
    store = tmp_path / "cache-outcomes.jsonl"
    archived_segments(store, 2)
    active_records(store, first=2, count=2)

    stream = outcome_history.open_history_beside(store)

    expected = tuple(NOW - dt.timedelta(minutes=index) for index in range(4))
    assert observed(tuple(stream)) == expected, "the first pass covers the history"
    assert observed(tuple(stream)) == expected, "the second pass repeats it"


def test_an_uncommitted_active_row_is_not_history(tmp_path: Path) -> None:
    """An unterminated active row is the writer's append in flight.

    An interrupted append can leave a complete row without its newline. The
    writer removes that remainder before its next append and `read_store`
    decodes only up to the last newline, so the row was never committed. A
    history read that decoded the frozen bytes as they stand would hand
    replay and dataset snapshots evidence the ordinary reader never returns.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    active_records(store, first=0, count=2)
    in_flight = outcome_record_encode.encode_line(records(2, 1)[0])
    with SegmentLayout.beside(store).active.open("ab") as handle:
        handle.write(in_flight.encode())

    stream = outcome_history.open_history_beside(store)

    assert observed(tuple(stream)) == (
        NOW,
        NOW - dt.timedelta(minutes=1),
    ), "the uncommitted row is not history"


def test_a_pinned_archive_that_changed_fails_closed(tmp_path: Path) -> None:
    """A segment replaced after the pin is refused rather than half-read."""
    store = tmp_path / "cache-outcomes.jsonl"
    archived_segments(store, 1)
    layout = SegmentLayout.beside(store)
    stream = outcome_history.open_history_beside(store)
    archive = layout.archive_dir / archive_name(1)
    archive.write_bytes(archive.read_bytes() + b"x")

    with pytest.raises(outcome_history.HistoryFaultError) as raised:
        tuple(stream)

    assert raised.value.reason == "unsettled", "the changed pin is a bounded fault"


def test_an_unselected_domain_is_not_decoded(tmp_path: Path) -> None:
    """A read of one domain does not decode another domain's segments.

    The damaged trailer belongs to the retired domain, so the newest
    domain's rows are still readable while an all-salts read reports it.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    write_store(store, (window(0, 1),), history_policy(1))
    (tmp_path / SALT_NAME).unlink()
    active_records(store, first=2, count=1)
    layout = SegmentLayout.beside(store)
    retired = sorted(layout.sealed_dir.glob("*.jsonl"))
    assert len(retired) == 1, "the retired domain left one sealed segment"
    retired[0].write_bytes(retired[0].read_bytes() + b"not-json\n")

    newest = outcome_history.read_history_beside(store)

    assert len(newest.records) == 1, "the newest domain remains readable"
    with pytest.raises(outcome_history.HistoryFaultError):
        outcome_history.read_history_beside(
            store, outcome_history.HistorySelection(all_salts=True)
        )


def test_history_reads_archived_and_active_records_in_append_order(
    tmp_path: Path,
) -> None:
    """Archived rows and active rows share one append-ordered history."""
    store = tmp_path / "cache-outcomes.jsonl"
    archived_segments(store, 3)
    active_records(store, first=3, count=2)

    active_only = typ.cast("DecodedStore", read_store(store))
    history = outcome_history.read_history_beside(store)

    assert len(active_only.records) == 2, (
        "the active file alone holds only the newest rows"
    )
    assert observed(history.records) == tuple(
        NOW - dt.timedelta(minutes=index) for index in range(5)
    ), "history reads archived rows before active rows without a gap"


def test_all_salts_names_every_domain_it_reads(tmp_path: Path) -> None:
    """A multi-domain history reports the salts its counts span, oldest first."""
    store = tmp_path / "cache-outcomes.jsonl"
    write_store(store, (window(0, 1),), history_policy(1))
    first = typ.cast("DecodedStore", read_store(store)).header.salt_id
    (tmp_path / SALT_NAME).unlink()
    active_records(store, first=2, count=1)
    second = typ.cast("DecodedStore", read_store(store)).header.salt_id
    assert first != second, "the fixture activates a new salt"

    every = outcome_history.read_history_beside(
        store, outcome_history.HistorySelection(all_salts=True)
    )

    assert every.salt_ids == (first, second), "the domains are named oldest first"


def test_history_defaults_to_the_newest_salt_domain(tmp_path: Path) -> None:
    """A salt change starts a new domain; the default reads that domain."""
    store = tmp_path / "cache-outcomes.jsonl"
    policy = history_policy(1)
    write_store(store, (window(0, 1), window(1, 1)), policy)
    (tmp_path / SALT_NAME).unlink()
    active_records(store, first=2, count=2)
    newest_salt = typ.cast("DecodedStore", read_store(store)).header.salt_id

    default = outcome_history.read_history_beside(store)
    selected = outcome_history.read_history_beside(
        store, outcome_history.HistorySelection(salt_id=newest_salt)
    )
    every = outcome_history.read_history_beside(
        store, outcome_history.HistorySelection(all_salts=True)
    )

    assert len(default.records) == 2, "the newest domain is the default"
    assert default.header.salt_id == newest_salt, "the newest header is reported"
    assert observed(default.records) == observed(selected.records), (
        "an explicit newest-salt selection matches the default"
    )
    assert len(every.records) == 4, "every domain is read in append order"


def test_history_reports_an_unknown_salt(tmp_path: Path) -> None:
    """A salt no retained segment carries is a closed fault."""
    store = tmp_path / "cache-outcomes.jsonl"
    archived_segments(store, 1)

    with pytest.raises(outcome_history.HistoryFaultError) as raised:
        outcome_history.read_history_beside(
            store, outcome_history.HistorySelection(salt_id="0" * 64)
        )

    assert raised.value.reason == "unknown_salt", "the reason is closed"


def test_history_reports_a_corrupt_archive(tmp_path: Path) -> None:
    """A damaged archive is a closed fault, not a shortened history."""
    store = tmp_path / "cache-outcomes.jsonl"
    archived_segments(store, 1)
    archive = next((tmp_path / "cache-outcomes.archive").glob("*.jsonl.xz"))
    archive.write_bytes(b"\x00" * 16)

    with pytest.raises(outcome_history.HistoryFaultError) as raised:
        outcome_history.read_history_beside(store)

    assert raised.value.reason in {"bad_xz", "truncated_xz"}, "the fault is bounded"


def test_history_reports_a_missing_store(tmp_path: Path) -> None:
    """A store with no active file and no segments has no history."""
    with pytest.raises(FileNotFoundError):
        outcome_history.read_history_beside(tmp_path / "absent.jsonl")
