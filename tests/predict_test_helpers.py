"""Shared builders for the prediction benchmark's tests."""

import dataclasses as dc
import datetime as dt
import hashlib
import json
import tempfile
import typing as typ
from pathlib import Path

import pytest
from outcome_test_helpers import OBSERVED_AT, cache_key

from buildlimits import outcome_record_codec, outcome_store_io
from buildlimits.outcome_record_encode import encode_line
from buildlimits.outcome_record_format import (
    FORMAT_VERSION,
    DecodedStore,
    OutcomeRecord,
    StoreHeader,
)
from buildlimits.predict import dataset_cli
from buildlimits.predict.accounting import HistoryLoss
from buildlimits.predict.config_loader import load_config
from buildlimits.predict.dataset import Snapshot, Source, build_snapshot
from buildlimits.predict.events import Event
from buildlimits.predict.journal_provenance import (
    JournalCounts,
    JournalUnavailable,
    count_journal,
)
from buildlimits.predict.records import decode_history
from buildlimits.predict.snapshot_store import load_snapshot, save_snapshot
from buildlimits.predict.synthetic import identity, stationary

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import Origin, StoreRecord
    from buildlimits.outcome_vocabulary import CacheResult
    from buildlimits.predict.config import Config
    from buildlimits.predict.events import Label

STORE_HEADER = StoreHeader("fixed-salt", dt.datetime(2026, 10, 24, tzinfo=dt.UTC))

ACTIVE_FILE_ONLY = HistoryLoss.active_file_only()
"""The scope of a test that decodes one in-memory store file.

A test builds its history from a single decoded file, so it has read the
active file and nothing else. Stating that scope explicitly is what
`build_snapshot` requires: the snapshot records what the reader established,
never an assumed default.
"""


def hand_event(  # ruff: ignore[too-many-arguments]  # one keyword per scenario value; pylint: disable=too-many-arguments
    index: int,
    *,
    observed_at: dt.datetime,
    admitted_at: dt.datetime | None,
    label: Label,
    verified: bool = True,
    attempt: str | None = None,
) -> Event:
    """Return one hand-built verified event for an ordering or weighting case.

    The identity is stable for the given index, so a stream built from
    consecutive indices never repeats one unless a test means it to. The
    observation time is verified by default: a hand-built scenario states the
    moment its label was known, so it is proof rather than a store row's lower
    bound. A test about the unverified case passes ``verified=False``. The
    attempt identity is derived from the event's own content, so two different
    events never collide and an exact repeat is an idempotent duplicate.

    Returns
    -------
    Event
        The event, with the git origin and a one-second runtime.
    """
    derived = hashlib.sha256(
        f"{index}|{admitted_at}|{observed_at}|{label}".encode()
    ).hexdigest()[:16]
    return Event(
        identity=identity(index),
        origin="git",
        admitted_at=admitted_at,
        observed_at=observed_at,
        label=label,
        runtime_seconds=1.0,
        label_observed_verified=verified,
        completed_at=observed_at,
        attempt=f"a{derived}" if attempt is None else attempt,
        supervisor_generation="test-generation",
    )


def predict_config(**changes: object) -> Config:
    """Return the pinned config with test-speed overrides applied.

    The bootstrap is small and the verdict thresholds are low, so a behavioural
    test can walk a synthetic stream quickly. ``changes`` overrides any
    parameter after those defaults.

    Returns
    -------
    Config
        The parameters to run one test with.
    """
    base = dc.replace(
        load_config(),
        minimum_labelled_events=100,
        minimum_scored_events=1,
        bootstrap_resamples=100,
    )
    return dc.replace(base, **changes)


def write_json(path: Path, document: object) -> Path:
    """Write one JSON document as text and return its path.

    Returns
    -------
    Path
        The path that was written.
    """
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    return path


def read_results(output: Path) -> tuple[dict[str, typ.Any], str]:
    """Read the two deterministic result files one run wrote.

    Returns
    -------
    tuple[dict[str, object], str]
        The parsed results document and the rendered Markdown.
    """
    document = json.loads((output / "results.json").read_text(encoding="utf-8"))
    markdown = (output / "results.md").read_text(encoding="utf-8")
    return document, markdown


def outcome_record(index: int, outcome: CacheResult, key: object) -> OutcomeRecord:
    """Return one attributed record at a distinct label time.

    Returns
    -------
    OutcomeRecord
        A record whose origin rotates over the three known origins.
    """
    observed_at = OBSERVED_AT + dt.timedelta(seconds=30 * index)
    return OutcomeRecord(
        key=typ.cast("typ.Any", key),
        outcome=outcome,
        origin=typ.cast("Origin", ("registry", "git", "path")[index % 3]),
        runtime_seconds=1.0 + index / 100,
        observed_at=observed_at,
        parser_class="cacheable",
        child_sighting="unavailable",
        child_samples=0,
        label_time_provenance="verified",
        completed_at=observed_at,
        attempt=f"g{index}",
        supervisor_generation="fixed-generation",
    )


def write_store(path: Path, count: int) -> None:
    """Write a store alternating hits and misses, with unknowns mixed in."""
    records = tuple(
        outcome_record(
            index,
            "unattributed" if index % 7 == 0 else ("hit" if index % 2 else "miss"),
            dc.replace(cache_key(), argument_digest=f"{index:064x}"),
        )
        for index in range(count)
    )
    outcome_store_io.replace_store(path, STORE_HEADER, typ.cast("typ.Any", records))


def write_admitted_store(path: Path, count: int) -> None:
    """Write a store whose rows each carry their own admission time.

    The label times keep their fixed spacing, and every admission time is two
    seconds earlier, so each row is a genuine pre-admission observation rather
    than a time derived from when the row was written.
    """
    records = []
    for index in range(count):
        labelled = index % 7 != 0
        record = outcome_record(
            index,
            "unattributed" if not labelled else ("hit" if index % 2 else "miss"),
            dc.replace(cache_key(), argument_digest=f"{index:064x}"),
        )
        records.append(
            dc.replace(
                record,
                outcome_reason="none" if labelled else "mixed_window",
                outcome_attribution="solo" if labelled else "none",
                admitted_at=record.observed_at - dt.timedelta(seconds=2),
            )
        )
    outcome_store_io.replace_store(
        path, STORE_HEADER, typ.cast("typ.Any", tuple(records))
    )


def write_admission_only_store(path: Path, count: int) -> None:
    """Write a store with admission times but no verified availability.

    This is exactly the shape a version 2 recorder produced: the decision is
    recorded at admission, and the label has a completion-derived observation
    time but no moment at which the observer accepted the reading. A history of
    these rows must report the availability gate, not the admission gate.
    """
    records = []
    for index in range(count):
        labelled = index % 7 != 0
        record = outcome_record(
            index,
            "unattributed" if not labelled else ("hit" if index % 2 else "miss"),
            dc.replace(cache_key(), argument_digest=f"{index:064x}"),
        )
        records.append(
            dc.replace(
                record,
                outcome_reason="none" if labelled else "mixed_window",
                outcome_attribution="solo" if labelled else "none",
                admitted_at=record.observed_at - dt.timedelta(seconds=2),
                completed_at=None,
                attempt=None,
                supervisor_generation=None,
            )
        )
    outcome_store_io.replace_store(
        path, STORE_HEADER, typ.cast("typ.Any", tuple(records))
    )


def write_admission_only_snapshot(tmp_path: Path, count: int) -> Path:
    """Snapshot a store that records decisions but no availability.

    Returns
    -------
    Path
        The snapshot the benchmark can be pointed at.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    snapshot = tmp_path / "history.jsonl"
    write_admission_only_store(store, count)
    dataset_cli.main([
        "--store",
        str(store),
        "--output",
        str(snapshot),
        "--skip-journal",
    ])
    return snapshot


def write_admitted_store_snapshot(tmp_path: Path, count: int) -> Path:
    """Snapshot a store whose retained rows carry admission times.

    Returns
    -------
    Path
        The snapshot the benchmark can be pointed at.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    snapshot = tmp_path / "history.jsonl"
    write_admitted_store(store, count)
    dataset_cli.main([
        "--store",
        str(store),
        "--output",
        str(snapshot),
        "--skip-journal",
    ])
    return snapshot


def record_events(
    records: tuple[StoreRecord, ...],
    *,
    now: dt.datetime,
    version: int = FORMAT_VERSION,
) -> tuple[Event, ...]:
    """Snapshot one reading's records and load the events back.

    A test that inspects the events a recorder emitted must read them through
    the file the dataset command writes, not through the in-memory rows: only
    the written document states each event's label-time provenance. The rows
    therefore travel through the real encoder and codec at ``version``, so a
    test that claims a version's behaviour has exercised that version's shapes.

    Parameters
    ----------
    records : tuple[StoreRecord, ...]
        The records one reading emitted, outcome rows and summary included.
    now : datetime
        The moment the reading returned, used as the store's write time.
    version : int
        The document version to write and read back. A version 2 document
        cannot carry a provenance marker, so a row that states one must be
        stripped before it is written there.

    Returns
    -------
    tuple[Event, ...]
        The loaded event stream, in store append order.
    """
    header = StoreHeader("fixed-salt", now, version=version)
    document = "\n".join((
        encode_line(header),
        *(
            encode_line(_as_version_row(record, version), version=version)
            for record in records
        ),
    ))
    decoded = outcome_record_codec.decode_lines(document + "\n")
    if not isinstance(decoded, DecodedStore):
        pytest.fail(f"the fixture's own document did not decode: {decoded}")
    snapshot = build_snapshot(
        decode_history(decoded),
        now=now,
        history_loss=HistoryLoss.active_file_only(),
    )
    # The directory is removed on both paths: a leaked one per call would
    # accumulate for the whole session, since nothing else owns it.
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "history.jsonl"
        save_snapshot(path, snapshot)
        return load_snapshot(path).events


_PROVENANCE_VERSION = 3
"""The first store version that states a row's label-time provenance."""


def _as_version_row(record: StoreRecord, version: int) -> StoreRecord:
    """Reproduce the row shape the named document version actually wrote.

    A version 1 or 2 release could not state label-time provenance, and a
    version 3 release could not state a generation digest, so a test that asks
    what such a history looks like must strip the evidence its own recorder now
    sets. Writing it as-is would be refused, which is the encoder correctly
    refusing to record proof a document cannot carry.

    Returns
    -------
    StoreRecord
        The record as that version would have written it.
    """
    if not isinstance(record, OutcomeRecord):
        return record
    if version >= FORMAT_VERSION:
        return record
    stripped = dc.replace(record, generation_digest=None)
    if version >= _PROVENANCE_VERSION:
        return stripped
    return dc.replace(stripped, label_time_provenance=None)


def write_snapshot(tmp_path: Path, count: int) -> Path:
    """Write a store of ``count`` records and snapshot it without the journal.

    Returns
    -------
    Path
        The snapshot the benchmark can be pointed at.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    snapshot = tmp_path / "history.jsonl"
    write_store(store, count)
    dataset_cli.main([
        "--store",
        str(store),
        "--output",
        str(snapshot),
        "--skip-journal",
    ])
    return snapshot


def write_admitted_snapshot(tmp_path: Path, count: int) -> Path:
    """Write a synthetic snapshot whose events carry admission times.

    The store records no admission time, so a store-derived snapshot can never
    be predicted. This helper supplies the other kind of history: known-property
    events, each admitted before its own label, so an end-to-end run exercises
    the full path instead of stopping at the admission check.

    Returns
    -------
    Path
        The snapshot the benchmark can be pointed at.
    """
    snapshot = Snapshot(
        created_at=dt.datetime(2026, 1, 2, tzinfo=dt.UTC),
        source=Source(
            salt_id="synthetic",
            written_at=STORE_HEADER.written_at,
            records_total=count,
            outcome_records=count,
            summary_records=0,
        ),
        journal=JournalUnavailable("journal query skipped"),
        events=stationary(count=count, rate=0.7, seed=5),
        gaps=(),
    )
    path = tmp_path / "admitted.jsonl"
    save_snapshot(path, snapshot)
    return path


def benchmark_argv(
    snapshot: Path, output: Path, config: Path | None = None
) -> list[str]:
    """Return the command line one end-to-end benchmark run needs.

    Every run names the same history and report directory and differs only in
    whether it supplies a parameter file, so the argument order lives here
    rather than being retyped at each call site.

    Returns
    -------
    list[str]
        The arguments, with ``--config`` left out when the defaults apply.
    """
    argv = ["--snapshot", str(snapshot), "--output", str(output)]
    if config is not None:
        argv += ["--config", str(config)]
    return argv


def write_fast_config(tmp_path: Path) -> Path:
    """Write a config that keeps an end-to-end run quick but well-founded.

    Returns
    -------
    Path
        The parameter file to pass to the benchmark command.
    """
    config: Config = dc.replace(
        load_config(),
        minimum_labelled_events=100,
        minimum_scored_events=10,
        bootstrap_resamples=50,
        weight_fit_steps=40,
        block_size=20,
    )
    return write_json(tmp_path / "config.json", dc.asdict(config))


def journal_note(*records: object, truncated: bool = False) -> JournalCounts:
    """Count one journal window over the supplied validated records.

    Both the provenance tests and the report tests need the same counting
    call; folding a window is the only thing they share, so it is built once
    here rather than restated per module.

    Returns
    -------
    JournalCounts
        The counts for the window.
    """
    return count_journal(
        typ.cast("typ.Any", records), since="4w", cap=1000, truncated=truncated
    )
