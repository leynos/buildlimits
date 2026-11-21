"""The H3 replay command reports bounded store and usage outcomes."""

import typing as typ
from pathlib import Path

import pytest
from outcome_store_fixture_helpers import seed_replay_store

from buildlimits import (
    outcome_evidence,
    outcome_history,
    outcome_replay,
    outcome_replay_cli,
)

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion


def test_missing_store_reports_insufficient_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A first run with no store is a report, not a command failure."""
    missing = tmp_path / "missing.jsonl"

    status = outcome_replay_cli.main(["h3", "--store", str(missing), "--json"])
    output = capsys.readouterr().out

    assert status == 0, "a missing store is an insufficient report"
    assert '"schema": 1' in output, "JSON is schema-labelled"
    assert '"covered": {"from": null, "until": null}' in output, (
        "an empty store reports no covered span"
    )


def test_h3_text_output_snapshot(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    snapshot: SnapshotAssertion,
) -> None:
    """The CLI text gives the verdict and sample sizes without private paths."""
    path = tmp_path / "store.jsonl"
    seed_replay_store(path)

    status = outcome_replay_cli.main(["h3", "--store", str(path)])
    output = capsys.readouterr().out

    assert status == 0, "a readable store produces a report"
    assert output == snapshot, "the bounded report stays stable"
    assert "covered 2026-10-24T12:00:00Z to 2026-10-24T12:00:00Z" in output, (
        "the text report states the covered span"
    )
    assert f"caveat: {outcome_evidence.QUIET_WINDOW_CAVEAT}" in output, (
        "every verdict states the quiet-window limit"
    )


@pytest.mark.parametrize(
    "document",
    [b"not-json\n", b'{"format":"buildlimits-cache-outcomes","version":2}\n'],
)
def test_invalid_store_names_reason_and_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], document: bytes
) -> None:
    """Corrupt and incompatible stores fail with a bounded reason."""
    path = tmp_path / "bad.jsonl"
    path.write_bytes(document)
    path.chmod(0o600)

    status = outcome_replay_cli.main(["h3", "--store", str(path)])

    assert status == 1, "invalid data is a command failure"
    assert "build-limits-cache-replay:" in capsys.readouterr().err, (
        'invalid_store_names_reason_...: "build-limits-cache-replay:" in caps'
    )


def test_unimplemented_hypothesis_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    """W1 accepts H2 and H3 while later hypotheses remain unavailable."""
    status = outcome_replay_cli.main(["h4"])

    assert status == 2, "unimplemented hypotheses are bad options"
    assert "usage:" in capsys.readouterr().err, (
        'unimplemented_hypothesis_ex...: "usage:" in capsys.readouterr().err'
    )


def test_a_replay_pass_reports_an_unsettled_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A pinned stream that moves is retried with one bounded notice."""
    attempts: list[int] = []

    def evaluate(_hypothesis: str, _records: object) -> outcome_replay.H3Report:
        """Fail the first pass as unsettled, then report an empty history."""
        attempts.append(1)
        if len(attempts) == 1:
            raise outcome_history.HistoryFaultError("unsettled")
        return outcome_replay.evaluate_h3([])

    monkeypatch.setattr(outcome_replay_cli, "_evaluate", evaluate)
    monkeypatch.setattr(
        outcome_replay_cli.outcome_history,
        "open_history_beside",
        lambda *_args, **_kwargs: object(),
    )

    outcome_replay_cli._history_report(
        tmp_path / "store.jsonl", outcome_history.HistorySelection(), "h3"
    )

    assert capsys.readouterr().err == "retry: history attempt 1 reason unsettled\n", (
        "the replay pass reports the retry it made"
    )
