"""The H3 replay command reports bounded store and usage outcomes."""

import datetime as dt
import typing as typ
from pathlib import Path

import pytest
from outcome_test_helpers import OBSERVED_AT, cache_key

from buildlimits import outcome_evidence, outcome_replay_cli, outcome_store_io
from buildlimits.outcome_record_format import (
    OutcomeRecord,
    StoreHeader,
    WindowSummary,
)

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion


def _write_store(path: Path) -> None:
    """Write one well-formed, private H3 fixture store."""
    records = (
        OutcomeRecord(
            key=cache_key(),
            outcome="hit",
            origin="registry",
            runtime_seconds=0.25,
            observed_at=OBSERVED_AT,
            parser_class="cacheable",
            child_sighting="unavailable",
            child_samples=0,
        ),
        WindowSummary(
            observed_at=OBSERVED_AT,
            server_version="0.14.0",
            window_groups=1,
            requests_not_compile=0,
            requests_not_cacheable=0,
            predicted_not_compile=0,
            predicted_declined=0,
            predicted_cacheable=1,
        ),
    )
    outcome_store_io.replace_store(
        path,
        StoreHeader("fixed-salt", dt.datetime(2026, 10, 24, tzinfo=dt.UTC)),
        records,
    )


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
    _write_store(path)

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
