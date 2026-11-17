"""End-to-end contracts for the dataset command."""

import datetime as dt
import typing as typ
from pathlib import Path

import pytest
from outcome_test_helpers import cache_key
from predict_test_helpers import STORE_HEADER, outcome_record, write_store

from buildlimits import outcome_store_io
from buildlimits.outcome_record_format import DecodedStore, StoreHeader
from buildlimits.predict import dataset_cli
from buildlimits.predict.snapshot_store import load_snapshot

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_vocabulary import CacheResult
    from buildlimits.predict.config import Config
    from buildlimits.predict.dataset import Snapshot


class _RaisingJournal:
    """A journal query that fails the way an unavailable journal does."""

    def __call__(self, **_options: object) -> dict[str, object]:
        """Report the journal as unreachable."""
        raise RuntimeError("journal unavailable")


def test_dataset_command_writes_a_redacted_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The dataset command extracts the store into a loadable snapshot."""
    store = tmp_path / "cache-outcomes.jsonl"
    snapshot = tmp_path / "history.jsonl"
    write_store(store, 40)

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


@pytest.mark.parametrize("alias", [False, True], ids=["same-path", "symlink"])
def test_dataset_command_refuses_to_write_over_its_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], *, alias: bool
) -> None:
    """The snapshot must never replace the store it was read from."""
    store = tmp_path / "history.jsonl"
    write_store(store, 4)
    output = store
    if alias:
        output = tmp_path / "alias.jsonl"
        output.symlink_to(store)
    before = store.read_bytes()

    status = dataset_cli.main([
        "--store",
        str(store),
        "--output",
        str(output),
        "--skip-journal",
    ])

    assert status == 2, "a destructive output path is a usage failure"
    assert "must not be the store being read" in capsys.readouterr().err, (
        "the refusal names the reason"
    )
    assert store.read_bytes() == before, "the store is untouched"


def test_dataset_command_refuses_an_unparsable_journal_span(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A malformed lookback is a usage failure, not a silent journal note."""
    store = tmp_path / "cache-outcomes.jsonl"
    snapshot = tmp_path / "history.jsonl"
    write_store(store, 4)

    status = dataset_cli.main([
        "--store",
        str(store),
        "--output",
        str(snapshot),
        "--journal-span",
        "soon",
    ])

    assert status == 2, "a malformed span is a usage failure"
    assert "--journal-span" in capsys.readouterr().err, "the option is named"
    assert not snapshot.exists(), "nothing is written behind the bad option"


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


def test_dataset_command_rejects_an_unknown_option() -> None:
    """Argparse exits, and the command reports it as a usage failure."""
    assert dataset_cli.main(["--no-such-option"]) == 2, "usage failure"


def test_dataset_command_reports_an_unreadable_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt packaged parameter file stops the command before it reads."""
    store = tmp_path / "cache-outcomes.jsonl"
    write_store(store, 4)

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


def test_dataset_command_reports_an_unwritable_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An output path that cannot be written fails with a status, not a traceback."""
    store = tmp_path / "cache-outcomes.jsonl"
    write_store(store, 4)
    blocked = tmp_path / "history.jsonl"
    blocked.mkdir()

    status = dataset_cli.main([
        "--store",
        str(store),
        "--output",
        str(blocked),
        "--skip-journal",
    ])

    assert status == 1, "an unwritable output is a command failure"
    assert "build-limits-predict-dataset:" in capsys.readouterr().err, (
        "the reason is reported"
    )


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
    write_store(store, 8)

    status = dataset_cli.main(
        ["--store", str(store), "--output", str(output)],
        edges=dataset_cli.Edges(journal=journal),
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
        outcome_record(0, typ.cast("CacheResult", "unattributed"), cache_key()),
        outcome_record(1, typ.cast("CacheResult", "declined"), cache_key()),
    )
    outcome_store_io.replace_store(store, STORE_HEADER, typ.cast("typ.Any", records))

    status = dataset_cli.main([
        "--store",
        str(store),
        "--output",
        str(tmp_path / "history.jsonl"),
        "--skip-journal",
    ])

    assert status == 0, "the snapshot is written"
    assert "insufficient data" in capsys.readouterr().err, "the reader is warned"


def test_the_command_reads_through_the_injected_clock_and_store(
    tmp_path: Path,
) -> None:
    """Injected edges let a test fix the time and observe the written snapshot."""
    clock = dt.datetime(2026, 3, 4, 5, 6, 7, tzinfo=dt.UTC)
    written: list[Snapshot] = []
    snapshot = tmp_path / "history.jsonl"

    def read_store(_store: Path) -> DecodedStore:
        """Stand in for the host store with an empty but decoded one."""
        return _decoded(())

    def write_snapshot(value: Snapshot, _output: Path) -> int:
        """Record the snapshot the command built instead of writing it."""
        written.append(value)
        return 0

    status = dataset_cli.main(
        [
            "--store",
            str(tmp_path / "store.jsonl"),
            "--output",
            str(snapshot),
            "--skip-journal",
        ],
        edges=dataset_cli.Edges(
            now=lambda: clock,
            read_store=read_store,
            write_snapshot=write_snapshot,
        ),
    )

    assert status == 0, "the command succeeds through the injected edges"
    assert written and written[0].created_at == clock, "the clock is the one supplied"
    assert not snapshot.exists(), "the writer, not the command, decides output"


def _decoded(records: tuple[object, ...]) -> DecodedStore:
    """Wrap records in a store header, as a decoded store file would."""
    written = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    return DecodedStore(
        StoreHeader("fixed-salt", written), typ.cast("typ.Any", records)
    )
