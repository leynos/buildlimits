"""CLI status, trust warnings and rendered metric-report snapshots."""

import typing as typ
from pathlib import Path

import pytest
from journal_fixtures import fixture_query

from buildlimits import metrics_cli, metrics_query

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from syrupy.assertion import SnapshotAssertion

FIXTURE = Path(__file__).parent / "fixtures" / "metrics.jsonl"
_QUERY = fixture_query(FIXTURE)


@pytest.mark.parametrize("output", [[], ["--json"]], ids=["text", "json"])
def test_cli_report(
    output: list[str], capsys: pytest.CaptureFixture[str], snapshot: SnapshotAssertion
) -> None:
    """Both report formats expose trust and preserve bounded aggregates."""
    assert metrics_cli.main(output, query=_QUERY) == 0, "report succeeds"
    captured = capsys.readouterr()
    assert not captured.err, "readable complete window has no warning"
    assert captured.out == snapshot, "bounded metrics snapshot"


def test_cli_truncation(
    capsys: pytest.CaptureFixture[str], snapshot: SnapshotAssertion
) -> None:
    """Cap equality is reported even after untrusted records are filtered."""
    assert (
        metrics_cli.main(
            ["--max-records", "11", "--source", "adapter", "--json"], query=_QUERY
        )
        == 0
    ), "truncated report succeeds"
    captured = capsys.readouterr()
    assert "cap reached" in captured.err, "truncation is explicit"
    assert captured == snapshot, "bounded metrics snapshot"


def test_cli_empty_window(
    capsys: pytest.CaptureFixture[str], snapshot: SnapshotAssertion
) -> None:
    """A readable empty window succeeds with an explicit warning."""
    assert (
        metrics_cli.main(
            query=lambda **_kw: metrics_query.Window((), 3600, False), argv=[]
        )
        == 0
    ), "empty report succeeds"
    captured = capsys.readouterr()
    assert "no accepted records" in captured.err, "empty window warning"
    assert captured == snapshot, "bounded metrics snapshot"


def test_cli_query_failure(capsys: pytest.CaptureFixture[str]) -> None:
    """An unreadable journal exits one and never prints a report."""

    def failed(**_options: object) -> typ.NoReturn:
        """Model a failed query without fabricating empty data."""
        msg = "journal access failed"
        raise metrics_query.MetricsQueryError(msg)

    assert metrics_cli.main([], query=failed) == 1, "query failure status"
    captured = capsys.readouterr()
    assert not captured.out and "journal access failed" in captured.err, (
        "stderr error only"
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["--since", "0h"],
        ["--max-records", "0"],
        ["--max-records", "100001"],
        ["--source", "private"],
        ["--unknown"],
    ],
)
def test_cli_usage_errors(
    argv: cabc.Sequence[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Invalid usage returns two without invoking the journal."""

    def forbidden(**_options: object) -> typ.NoReturn:
        """Fail the test if invalid usage reaches a transport."""
        msg = "invalid usage reached query"
        raise AssertionError(msg)

    assert metrics_cli.main(argv, query=forbidden) == 2, "usage status"
    assert capsys.readouterr().err, "usage error explains the invalid option"
