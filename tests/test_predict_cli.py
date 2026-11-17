"""End-to-end contracts for the dataset and benchmark commands."""

import dataclasses as dc
import datetime as dt
import json
import typing as typ
from pathlib import Path
from unittest import mock

import pytest
from outcome_test_helpers import OBSERVED_AT, cache_key

from buildlimits import outcome_store_io
from buildlimits.outcome_record_format import OutcomeRecord, StoreHeader
from buildlimits.predict import benchmark_cli, dataset_cli
from buildlimits.predict.config import Config, load_config
from buildlimits.predict.snapshot_store import load_snapshot

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_record_format import Origin
    from buildlimits.outcome_vocabulary import CacheResult

HEADER = StoreHeader("fixed-salt", dt.datetime(2026, 10, 24, tzinfo=dt.UTC))


class _RaisingJournal:
    """A journal query that fails the way an unavailable journal does."""

    def __call__(self, **_options: object) -> dict[str, object]:
        """Report the journal as unreachable."""
        raise RuntimeError("journal unavailable")


def _record(index: int, outcome: CacheResult, key: object) -> OutcomeRecord:
    """Return one attributed record at a distinct label time."""
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


def _write_store(path: Path, count: int) -> None:
    """Write a store alternating hits and misses, with unknowns mixed in."""
    records = tuple(
        _record(
            index,
            "unattributed" if index % 7 == 0 else ("hit" if index % 2 else "miss"),
            dc.replace(cache_key(), argument_digest=f"{index:064x}"),
        )
        for index in range(count)
    )
    outcome_store_io.replace_store(path, HEADER, typ.cast("typ.Any", records))


def _snapshot(tmp_path: Path, count: int) -> Path:
    """Write a store of ``count`` records and snapshot it without the journal."""
    store = tmp_path / "cache-outcomes.jsonl"
    snapshot = tmp_path / "history.jsonl"
    _write_store(store, count)
    dataset_cli.main([
        "--store",
        str(store),
        "--output",
        str(snapshot),
        "--skip-journal",
    ])
    return snapshot


def _fast_config(tmp_path: Path) -> Path:
    """Write a config that keeps an end-to-end run quick but well-founded."""
    config: Config = dc.replace(
        load_config(),
        minimum_labelled_events=100,
        minimum_scored_events=10,
        bootstrap_resamples=50,
        weight_fit_steps=40,
        block_size=20,
    )
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config.as_dict()) + "\n", encoding="utf-8")
    return path


def test_dataset_command_writes_a_redacted_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The dataset command extracts the store into a loadable snapshot."""
    store = tmp_path / "cache-outcomes.jsonl"
    snapshot = tmp_path / "history.jsonl"
    _write_store(store, 40)

    status = dataset_cli.main([
        "--store",
        str(store),
        "--output",
        str(snapshot),
        "--skip-journal",
    ])

    output = capsys.readouterr().out
    loaded = load_snapshot(snapshot)
    assert status == 0, "the snapshot is written"
    assert len(loaded.events) == 40, "every attributed row becomes an event"
    assert "events=40" in output, "the command reports what it wrote"
    assert "serde" not in snapshot.read_text(encoding="utf-8"), "redacted"


def test_dataset_command_reports_a_missing_store(tmp_path: Path) -> None:
    """A store that cannot be read is a command failure, not a snapshot."""
    status = dataset_cli.main([
        "--store",
        str(tmp_path / "absent.jsonl"),
        "--output",
        str(tmp_path / "history.jsonl"),
        "--skip-journal",
    ])

    assert status == 1, "missing store is fatal"
    assert not (tmp_path / "history.jsonl").exists(), "nothing written"


def test_benchmark_command_writes_both_result_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One run writes results.json, results.md and the timing appendix."""
    output = tmp_path / "out"
    snapshot = _snapshot(tmp_path, 400)
    capsys.readouterr()

    status = benchmark_cli.main([
        "--snapshot",
        str(snapshot),
        "--output",
        str(output),
        "--config",
        str(_fast_config(tmp_path)),
    ])

    printed = capsys.readouterr().out
    document = json.loads((output / "results.json").read_text(encoding="utf-8"))
    markdown = (output / "results.md").read_text(encoding="utf-8")
    assert status == 0, "the benchmark completes"
    assert document["status"] == "ok", "enough labels for a verdict"
    assert len(document["predictors"]) == 9, "six singles and three ensembles"
    assert "Held-out comparison" in markdown, "the comparison table is printed"
    assert "Held-out comparison" in printed, "the command prints the table"
    assert (output / "timings.json").exists(), "latency is reported separately"


def test_benchmark_defaults_report_insufficient_data_honestly(
    tmp_path: Path,
) -> None:
    """Too little history is stated with counts, never padded or faked."""
    output = tmp_path / "out"
    snapshot = _snapshot(tmp_path, 30)

    status = benchmark_cli.main([
        "--snapshot",
        str(snapshot),
        "--output",
        str(output),
    ])

    document = json.loads((output / "results.json").read_text(encoding="utf-8"))
    markdown = (output / "results.md").read_text(encoding="utf-8")
    assert status == 0, "an honest negative result is not a failure"
    assert document["status"] == "insufficient data", "status is explicit"
    assert "2000 required" in document["verdict"], "the threshold is reported"
    assert "Insufficient data" in markdown, "the report says so in prose"
    assert document["data"]["stats"]["labelled"] == 25, "counts are real"


def test_two_runs_produce_identical_result_files(tmp_path: Path) -> None:
    """The same snapshot and config give byte-identical results.json."""
    config = _fast_config(tmp_path)
    snapshot = _snapshot(tmp_path, 400)

    first = tmp_path / "first"
    second = tmp_path / "second"
    for output in (first, second):
        benchmark_cli.main([
            "--snapshot",
            str(snapshot),
            "--output",
            str(output),
            "--config",
            str(config),
        ])

    assert (first / "results.json").read_bytes() == (
        second / "results.json"
    ).read_bytes(), "deterministic results"
    assert (first / "results.md").read_bytes() == (
        second / "results.md"
    ).read_bytes(), "deterministic report"


def test_benchmark_reports_a_missing_snapshot(tmp_path: Path) -> None:
    """A run without a snapshot fails clearly instead of inventing history."""
    status = benchmark_cli.main([
        "--snapshot",
        str(tmp_path / "absent.jsonl"),
        "--output",
        str(tmp_path / "out"),
    ])

    assert status == 1, "missing snapshot is fatal"


def test_benchmark_reports_an_invalid_config(tmp_path: Path) -> None:
    """An unusable parameter file is a usage failure."""
    snapshot = tmp_path / "history.jsonl"
    snapshot.write_text("{}", encoding="utf-8")
    bad = tmp_path / "config.json"
    bad.write_text('{"config_version": 1}\n', encoding="utf-8")

    status = benchmark_cli.main([
        "--snapshot",
        str(snapshot),
        "--output",
        str(tmp_path / "out"),
        "--config",
        str(bad),
    ])

    assert status == 2, "invalid config is a usage failure"


@pytest.mark.parametrize(
    "command",
    [benchmark_cli.main, dataset_cli.main],
    ids=["benchmark", "dataset"],
)
def test_a_command_rejects_an_unknown_option(
    command: cabc.Callable[[list[str]], int],
) -> None:
    """Argparse exits, and the command reports it as a usage failure."""
    assert command(["--no-such-option"]) == 2, "usage failure"


@pytest.mark.parametrize(
    ("which", "run"),
    [
        pytest.param(lambda _name: None, None, id="no-git-binary"),
        pytest.param(
            lambda _name: "/usr/bin/git",
            mock.Mock(side_effect=OSError("git unavailable")),
            id="git-call-fails",
        ),
    ],
)
def test_git_sha_is_unknown_when_git_cannot_answer(
    monkeypatch: pytest.MonkeyPatch,
    which: cabc.Callable[[str], str | None],
    run: object,
) -> None:
    """A host without a working git still produces a report."""
    monkeypatch.setattr(benchmark_cli.shutil, "which", which)
    if run is not None:
        monkeypatch.setattr(benchmark_cli.subprocess, "run", run)

    assert benchmark_cli._git_sha() == "unknown", "explicit marker"


def test_dataset_command_reports_an_unreadable_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt packaged parameter file stops the command before it reads."""
    store = tmp_path / "cache-outcomes.jsonl"
    _write_store(store, 4)

    def refuse() -> Config:
        """Stand in for a parameter file this build cannot read."""
        raise ValueError("config.json is corrupt")

    monkeypatch.setattr(dataset_cli, "load_config", refuse)

    status = dataset_cli.main([
        "--store",
        str(store),
        "--output",
        str(tmp_path / "history.jsonl"),
        "--skip-journal",
    ])

    assert status == 2, "an unusable config is a usage failure"


def test_dataset_command_reports_a_rejected_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A store the decoder refuses is a failure with its reason stated."""
    store = tmp_path / "cache-outcomes.jsonl"
    store.write_text("not a store row\n", encoding="utf-8")

    status = dataset_cli.main([
        "--store",
        str(store),
        "--output",
        str(tmp_path / "history.jsonl"),
        "--skip-journal",
    ])

    assert status == 1, "a refused store is fatal"
    assert "rejected" in capsys.readouterr().err, "the reason is reported"
    assert not (tmp_path / "history.jsonl").exists(), "nothing written"


def _journal_snapshot(
    tmp_path: Path, journal: cabc.Callable[..., cabc.Mapping[str, object]]
) -> tuple[int, dict[str, object]]:
    """Snapshot eight rows using ``journal`` and return its status and note."""
    store = tmp_path / "cache-outcomes.jsonl"
    output = tmp_path / "history.jsonl"
    _write_store(store, 8)

    status = dataset_cli.main(
        ["--store", str(store), "--output", str(output)], journal=journal
    )
    return status, load_snapshot(output).journal


def test_dataset_command_survives_a_journal_that_raises(tmp_path: Path) -> None:
    """Journal provenance is context, so a journal failure cannot lose history."""
    status, note = _journal_snapshot(tmp_path, _RaisingJournal())

    assert status == 0, "the snapshot is still written"
    assert note == {"available": False, "reason": "journal query failed"}, (
        "the failure is recorded as provenance"
    )


def test_dataset_command_records_journal_counts(tmp_path: Path) -> None:
    """A journal that answers is recorded as provenance beside the snapshot."""
    status, note = _journal_snapshot(
        tmp_path, lambda **_options: {"available": True, "records": 12}
    )

    assert status == 0, "the snapshot is written"
    assert note == {"available": True, "records": 12}, "counts kept"


def test_dataset_command_warns_when_nothing_is_labelled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A store with only unattributed rows says the benchmark cannot run."""
    store = tmp_path / "cache-outcomes.jsonl"
    records = (
        _record(0, "unattributed", cache_key()),
        _record(1, "declined", cache_key()),
    )
    outcome_store_io.replace_store(store, HEADER, typ.cast("typ.Any", records))

    status = dataset_cli.main([
        "--store",
        str(store),
        "--output",
        str(tmp_path / "history.jsonl"),
        "--skip-journal",
    ])

    assert status == 0, "the snapshot is written"
    assert "insufficient data" in capsys.readouterr().err, "the reader is warned"
