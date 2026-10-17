"""CLI rendering of optional bounded pressure observations."""

import json
import typing as typ
from pathlib import Path

from journal_fixtures import fixture_query

from buildlimits import metrics_cli

if typ.TYPE_CHECKING:
    import pytest
    from syrupy.assertion import SnapshotAssertion

FIXTURE = Path(__file__).parent / "fixtures" / "metrics_pressure.jsonl"
_QUERY = fixture_query(FIXTURE)


def test_pressure_text_output(
    capsys: pytest.CaptureFixture[str], snapshot: SnapshotAssertion
) -> None:
    """Text output appends the pressure aggregate without weakening the trust header."""
    assert metrics_cli.main([], query=_QUERY) == 0, "a pressure report succeeds"
    captured = capsys.readouterr()
    assert not captured.err, "a complete readable pressure window needs no warning"
    assert "Supervisor: trusted user unit; adapters: self-reported" in captured.out, (
        "the existing source trust explanation remains present"
    )
    assert "pressure: " in captured.out, "the optional pressure aggregate is rendered"
    assert "history: " not in captured.out, "text output keeps detailed history in JSON"
    assert captured.out == snapshot, "pressure text report snapshot"


def test_pressure_json_output(
    capsys: pytest.CaptureFixture[str], snapshot: SnapshotAssertion
) -> None:
    """JSON exposes explicit zero and unknown knowledge alongside bounded history."""
    assert metrics_cli.main(["--json"], query=_QUERY) == 0, "JSON report succeeds"
    captured = capsys.readouterr()
    assert not captured.err, "the complete window needs no warning"
    result = json.loads(captured.out)
    current = result["pressure"]["descriptors"]["latest_observed"]
    assert current["budget"] == 0 and current["budget_known"] is False, (
        "effective zero and unknown baseline remain distinct JSON scalar types"
    )
    assert len(result["history"]) == 16, (
        "JSON history includes only accepted observations"
    )
    assert result["pressure"] == snapshot, "focused pressure JSON aggregate snapshot"


def test_pressure_cli_truncation_after_source_filter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Adapter-only output still reports a raw cap filled by trusted and rejected sources."""
    assert (
        metrics_cli.main(
            ["--json", "--source", "adapter", "--max-records", "17"], query=_QUERY
        )
        == 0
    ), "a capped readable report succeeds"
    captured = capsys.readouterr()
    assert "cap reached" in captured.err, "raw-cap truncation remains explicit"
    result = json.loads(captured.out)
    assert result["truncated"] is True and result["records"] == 3, (
        "source selection follows the seventeen-record raw cap"
    )
    assert result["pressure"]["adapters"]["busy_retries"]["count"] == 2, (
        "only reported adapter retry samples contribute"
    )
