"""Shared builders for the prediction benchmark's tests."""

import dataclasses as dc
import datetime as dt
import json
import typing as typ

from outcome_test_helpers import OBSERVED_AT, cache_key

from buildlimits import outcome_store_io
from buildlimits.outcome_record_format import OutcomeRecord, StoreHeader
from buildlimits.predict import dataset_cli
from buildlimits.predict.config_loader import load_config
from buildlimits.predict.dataset import Snapshot, Source
from buildlimits.predict.events import Event
from buildlimits.predict.journal_provenance import JournalUnavailable
from buildlimits.predict.snapshot_store import save_snapshot
from buildlimits.predict.synthetic import identity, stationary

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_record_format import Origin
    from buildlimits.outcome_vocabulary import CacheResult
    from buildlimits.predict.config import Config
    from buildlimits.predict.events import Label

STORE_HEADER = StoreHeader("fixed-salt", dt.datetime(2026, 10, 24, tzinfo=dt.UTC))


def hand_event(
    index: int,
    *,
    observed_at: dt.datetime,
    admitted_at: dt.datetime | None,
    label: Label,
) -> Event:
    """Return one hand-built event for an ordering or weighting scenario.

    The identity is stable for the given index, so a stream built from
    consecutive indices never repeats one unless a test means it to.

    Returns
    -------
    Event
        The event, with the git origin and a one-second runtime.
    """
    return Event(
        identity=identity(index),
        origin="git",
        admitted_at=admitted_at,
        observed_at=observed_at,
        label=label,
        runtime_seconds=1.0,
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
    return OutcomeRecord(
        key=typ.cast("typ.Any", key),
        outcome=outcome,
        origin=typ.cast("Origin", ("registry", "git", "path")[index % 3]),
        runtime_seconds=1.0 + index / 100,
        observed_at=OBSERVED_AT + dt.timedelta(seconds=30 * index),
        parser_class="cacheable",
        child_sighting="unavailable",
        child_samples=0,
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
