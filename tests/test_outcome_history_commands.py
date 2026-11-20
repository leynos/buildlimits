"""The replay and dataset commands reading archived history.

Both commands default to the writer's active file. ``--history`` widens them to
the whole retained history, so these tests check that archived rows reach the
replay report and the dataset snapshot, and that a damaged archive stops the
command rather than silently shortening what it reads.
"""

import typing as typ
from pathlib import Path

from history_fixtures import active_records, archived_segments

from buildlimits import outcome_history, outcome_replay, outcome_replay_cli
from buildlimits.predict import dataset_cli
from buildlimits.predict.snapshot_store import load_snapshot

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest

    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.outcome_replay import H3Report


def _archived_store(tmp_path: Path) -> Path:
    """Build a store holding one archived segment.

    Returns
    -------
    Path
        The store path the replay commands read.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    archived_segments(store, 1)
    return store


def _replay_archived_history(store: Path) -> int:
    """Run the archived-history H3 report and return its exit status.

    Returns
    -------
    int
        The command's exit status.
    """
    return outcome_replay_cli.main([
        "h3",
        "--store",
        str(store),
        "--history",
        "--json",
    ])


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


def test_replay_retries_a_history_that_moved_mid_pass(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass over a replaced snapshot is discarded and replayed.

    The retry opens a fresh pin, so a report never mixes rows from two
    snapshots of the store.
    """
    store = _archived_store(tmp_path)
    real = outcome_replay.evaluate_h3
    passes = 0

    def move_once(rows: cabc.Iterable[StoreRecord]) -> H3Report:
        """Fail the first pass as a replaced snapshot does."""
        nonlocal passes
        passes += 1
        if passes == 1:
            raise outcome_history.HistoryFaultError("unsettled")
        return real(rows)

    monkeypatch.setattr(outcome_replay, "evaluate_h3", move_once)

    status = _replay_archived_history(store)

    assert status == 0, "the retry produces a report"
    assert '"attributed_outcomes": 1' in capsys.readouterr().out, "the retry reads it"
    assert passes == 2, "exactly one pass was discarded"


def test_replay_does_not_retry_a_corrupt_history(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed decode fault is reported at once, not retried."""
    store = _archived_store(tmp_path)
    passes = 0

    def refuse(_rows: object) -> object:
        """Fail the only pass as a damaged segment would."""
        nonlocal passes
        passes += 1
        raise outcome_history.HistoryFaultError("corrupt")

    monkeypatch.setattr(outcome_replay, "evaluate_h3", refuse)

    status = _replay_archived_history(store)

    assert status == 1, "a damaged history is a command failure"
    assert "corrupt" in capsys.readouterr().out, "the reason is reported"
    assert passes == 1, "a closed fault is never retried"


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
