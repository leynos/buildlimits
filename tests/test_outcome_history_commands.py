"""The replay and dataset commands reading archived history.

Both commands default to the writer's active file. ``--history`` widens them to
the whole retained history, so these tests check that archived rows reach the
replay report and the dataset snapshot, and that a damaged archive stops the
command rather than silently shortening what it reads.
"""

import typing as typ
from pathlib import Path

from history_fixtures import active_records, archived_segments

from buildlimits import outcome_replay_cli
from buildlimits.predict import dataset_cli
from buildlimits.predict.snapshot_store import load_snapshot

if typ.TYPE_CHECKING:
    import pytest


def test_replay_reads_archived_history_when_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--history`` lets the replay command see archived rows."""
    store = tmp_path / "cache-outcomes.jsonl"
    archived_segments(store, 2)
    active_records(store, first=2, count=1)

    status = outcome_replay_cli.main([
        "h2",
        "--store",
        str(store),
        "--history",
        "--json",
    ])
    output = capsys.readouterr().out

    assert status == 0, "a readable history produces a report"
    assert '"attributed_outcomes": 3' in output, "archived rows are replayed"


def test_replay_rejects_an_unknown_salt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A salt the store never carried exits one with a bounded reason."""
    store = tmp_path / "cache-outcomes.jsonl"
    archived_segments(store, 1)

    status = outcome_replay_cli.main([
        "h2",
        "--store",
        str(store),
        "--salt-id",
        "0" * 64,
    ])

    assert status == 1, "an unknown domain is a command failure"
    assert "unknown_salt" in capsys.readouterr().err, "the reason is reported"


def test_replay_refuses_two_domain_selectors(tmp_path: Path) -> None:
    """``--salt-id`` and ``--all-salts`` cannot be combined."""
    status = outcome_replay_cli.main([
        "h2",
        "--store",
        str(tmp_path / "store.jsonl"),
        "--salt-id",
        "abc",
        "--all-salts",
    ])

    assert status == 2, "conflicting selectors are a usage failure"


def test_dataset_reads_archived_history_when_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--history`` lets the snapshot command include archived rows."""
    store = tmp_path / "cache-outcomes.jsonl"
    snapshot = tmp_path / "history.jsonl"
    archived_segments(store, 2)
    active_records(store, first=2, count=1)

    status = dataset_cli.main([
        "--store",
        str(store),
        "--output",
        str(snapshot),
        "--skip-journal",
        "--history",
    ])

    assert status == 0, "the snapshot is written"
    assert "events=3" in capsys.readouterr().out, "archived rows become events"
    assert len(load_snapshot(snapshot).events) == 3, "the snapshot holds them"


def test_dataset_reports_a_rejected_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A corrupt archive is a bounded failure, never a partial snapshot."""
    store = tmp_path / "cache-outcomes.jsonl"
    snapshot = tmp_path / "history.jsonl"
    archived_segments(store, 1)
    archive = next((tmp_path / "cache-outcomes.archive").glob("*.jsonl.xz"))
    archive.write_bytes(b"\x00" * 16)

    status = dataset_cli.main([
        "--store",
        str(store),
        "--output",
        str(snapshot),
        "--skip-journal",
        "--history",
    ])

    assert status == 1, "a rejected history is fatal"
    assert "rejected as" in capsys.readouterr().err, "the reason is reported"
    assert not snapshot.exists(), "nothing is written behind a fault"
