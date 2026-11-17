"""Shared builders for the prediction benchmark's tests."""

import dataclasses as dc
import datetime as dt
import json
import typing as typ

from outcome_test_helpers import OBSERVED_AT, cache_key

from buildlimits import outcome_store_io
from buildlimits.outcome_record_format import OutcomeRecord, StoreHeader
from buildlimits.predict import dataset_cli
from buildlimits.predict.config import load_config

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_record_format import Origin
    from buildlimits.outcome_vocabulary import CacheResult
    from buildlimits.predict.config import Config

STORE_HEADER = StoreHeader("fixed-salt", dt.datetime(2026, 10, 24, tzinfo=dt.UTC))


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
    return write_json(tmp_path / "config.json", config.as_dict())
