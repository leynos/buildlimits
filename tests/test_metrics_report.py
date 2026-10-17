"""Trust, bounds and pure aggregation contracts for journal metric windows."""

import json
import subprocess
import typing as typ
from pathlib import Path

import pytest
from journal_fixtures import journal_runner

from buildlimits.metrics import EVENTS
from buildlimits.metrics_query import (
    MAX_RECORDS,
    QUERY_FIELDS,
    MetricsQueryError,
    query,
)
from buildlimits.metrics_report import percentiles, report

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from syrupy.assertion import SnapshotAssertion

FIXTURE = Path(__file__).parent / "fixtures" / "metrics.jsonl"


def _unexpected(_argv: object) -> typ.NoReturn:
    """Fail if validation lets invalid options reach the journal transport."""
    pytest.fail("invalid options called the runner")


def test_metrics_report(snapshot: SnapshotAssertion) -> None:
    """The report ignores an untrusted unit and separates adapter measurements."""
    window = query(runner=journal_runner(FIXTURE.read_text()))
    result = typ.cast("dict[str, typ.Any]", report(window))
    assert len(window.records) == 10, "untrusted supervisor record rejected"
    assert result["requests"]["outcomes"] == {"completed": 1, "refused": 1}, (
        "terminal counts"
    )
    assert result["requests"]["per_minute"] == round(2 / 60, 6), (
        "configured window rate"
    )
    assert result["cache"]["direct_fallback_ratio"] == pytest.approx(0.5), (
        "eligible admission denominator"
    )
    assert result["gauges"]["queue_sample_mean"] == pytest.approx(1.0), (
        "sample mean of present gauges"
    )
    assert result["adapters"]["self_reported"] is True, "adapter trust explicit"
    assert result == snapshot, "bounded metrics snapshot"


def test_query_cap_precedes_source_filter(snapshot: SnapshotAssertion) -> None:
    """Filling the raw cap reports truncation even when records are rejected."""
    output = FIXTURE.read_text()
    calls: list[cabc.Sequence[str]] = []

    def runner(argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Record the bounded command and return fixture records.

        Returns
        -------
        subprocess.CompletedProcess[str]
            The fixture transport response.
        """
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, output, "")

    window = query(since="30m", max_records=11, source="adapter", runner=runner)
    assert window.truncated and len(window.records) == 1, (
        "raw cap, then source filtering"
    )
    selected = [arg for arg in calls[0] if arg.startswith("--output-fields=")]
    assert len(selected) == 1, "journal query selects only schema and trust fields"
    assert "--since=-30m" in calls[0] and "--lines=11" in calls[0], (
        "bounded lookback and cap"
    )
    assert selected[0] == f"--output-fields={','.join(sorted(QUERY_FIELDS))}", (
        "fixed field whitelist"
    )
    assert calls == snapshot(name="journal_command"), "bounded journal command"
    assert report(window) == snapshot, "bounded metrics snapshot"


@pytest.mark.parametrize("output", ["not-json", "[]"])
def test_malformed_journal_is_a_query_error(output: str) -> None:
    """An unreadable JSON stream is never represented as an empty report."""
    with pytest.raises(MetricsQueryError):
        query(runner=journal_runner(output))


@pytest.mark.parametrize("status", [0, 1])
def test_journal_access_diagnostics_are_errors(status: int) -> None:
    """Permission diagnostics cannot masquerade as a readable empty window."""
    with pytest.raises(MetricsQueryError, match="verify journal access"):
        query(
            runner=lambda argv: subprocess.CompletedProcess(
                argv, status, "", "permission denied"
            )
        )


def test_invalid_native_fields_do_not_become_labels() -> None:
    """Arrays, unknown tokens and private fields do not reach report history."""
    raw = json.loads(FIXTURE.read_text().splitlines()[4])
    raw.update(
        BUILD_LIMITS_OUTCOME="/secret/crate",
        BUILD_LIMITS_WAIT_SECONDS=["1", "2"],
        BUILD_LIMITS_PATH="/private",
        BUILD_LIMITS_HELD="-1",
    )
    window = query(runner=journal_runner(json.dumps(raw)))
    assert len(window.records) == 1, "valid source record retained"
    assert window.records[0].fields == {
        "reason": "none",
        "cancellation": "none",
        "runtime_seconds": "5.000",
    }, "only bounded fields survive"
    assert "secret" not in str(report(window)) and "private" not in str(
        report(window)
    ), "no identifier leakage"


@pytest.mark.parametrize(
    ("values", "expected"), [([], None), ([1.0], 1.0), (list(range(1, 101)), 90)]
)
def test_nearest_rank(values: list[float], expected: float | None) -> None:
    """Nearest rank uses the observed sample without interpolation."""
    assert percentiles(values)["p90"] == expected, "nearest-rank p90"


@pytest.mark.parametrize(
    "options",
    [
        {"since": "0h"},
        {"since": "today"},
        {"max_records": 0},
        {"max_records": 100001},
        {"max_records": True},
        {"max_records": False},
        {"max_records": 1.0},
        {"max_records": "1"},
        {"source": "other"},
    ],
)
def test_query_options_are_bounded(options: dict[str, typ.Any]) -> None:
    """Invalid options fail before the transport can execute."""
    with pytest.raises(ValueError, match="--"):
        query(**options, runner=_unexpected)


def test_empty_window_has_no_invented_samples(snapshot: SnapshotAssertion) -> None:
    """A readable empty window has zero rates and absent distributions."""
    window = query(runner=journal_runner(""))
    assert not window.truncated and not window.records, "empty readable result"
    assert report(window) == snapshot, "bounded metrics snapshot"


def test_transport_failure_is_a_query_error() -> None:
    """A missing journal executable cannot produce a successful empty report."""

    def failed(_argv: object) -> typ.NoReturn:
        """Raise the operating system's executable error."""
        msg = "journalctl unavailable"
        raise FileNotFoundError(msg)

    with pytest.raises(MetricsQueryError, match="cannot execute"):
        query(runner=failed)


def test_output_budget_failure_is_explicit() -> None:
    """Byte-budget failures remain query errors instead of partial reports."""
    from buildlimits.metrics_transport import JournalOutputError

    cause = JournalOutputError("journal query exceeded its output byte limit")

    def rejected(_argv: object) -> typ.NoReturn:
        """Raise the specific bounded-output failure."""
        raise cause

    with pytest.raises(MetricsQueryError, match="output byte limit") as caught:
        query(runner=rejected)
    assert str(caught.value) == str(cause), "specific transport message retained"
    assert caught.value.__cause__ is cause, "specific transport cause retained"


def test_blank_lines_do_not_count_towards_the_record_cap() -> None:
    """Blank separators leave the real-record cap and truncation flag intact."""
    output = "\n \n" + FIXTURE.read_text().splitlines()[0] + "\n\n"
    window = query(max_records=1, runner=journal_runner(output))
    assert len(window.records) == 1 and window.truncated, "only one real record counted"
    empty = query(max_records=1, runner=journal_runner("\n \n"))
    assert not empty.records and not empty.truncated, "blank-only output is empty"


@pytest.mark.parametrize("max_records", [1, MAX_RECORDS])
def test_query_accepts_record_cap_boundaries(max_records: int) -> None:
    """Both inclusive limits reach the runner with the requested raw cap."""
    calls: list[cabc.Sequence[str]] = []

    def runner(argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Capture validated arguments and return one trusted record.

        Returns
        -------
        subprocess.CompletedProcess[str]
            A successful single-record response.
        """
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, FIXTURE.read_text().splitlines()[0], ""
        )

    window = query(max_records=max_records, runner=runner)
    assert len(calls) == 1 and f"--lines={max_records}" in calls[0], (
        "inclusive cap passed"
    )
    assert len(window.records) == 1, "trusted record accepted"
    assert window.truncated is (max_records == 1), "truncation uses raw count"


@pytest.mark.parametrize(
    ("since", "max_records", "source", "message"),
    [
        ("0h", False, "other", "--since must be a positive duration such as 30m or 1h"),
        ("1h", False, "other", "--max-records must be between 1 and 100000"),
        ("1h", 1, "other", "--source must be all, supervisor or adapter"),
    ],
)
def test_query_validation_order(
    since: str, max_records: int, source: str, message: str
) -> None:
    """Earlier invalid options keep their exact errors and prevent execution."""
    with pytest.raises(ValueError, match="--") as caught:
        query(since=since, max_records=max_records, source=source, runner=_unexpected)
    assert str(caught.value) == message, "validation order and message retained"


@pytest.mark.parametrize("status", [1, -1])
def test_nonzero_status_without_diagnostics_is_a_query_error(status: int) -> None:
    """A silent process failure cannot masquerade as an empty window."""
    with pytest.raises(MetricsQueryError, match="verify journal access") as caught:
        query(runner=lambda argv: subprocess.CompletedProcess(argv, status, "", ""))
    assert str(caught.value) == (
        "user-journal query failed; verify journal access with journalctl --user"
    ), "process failure message retained"


def test_whitespace_stderr_is_a_successful_query() -> None:
    """Blank diagnostics do not reject otherwise valid journal records."""
    window = query(
        runner=lambda argv: subprocess.CompletedProcess(
            argv, 0, FIXTURE.read_text().splitlines()[0], " \t\n"
        )
    )
    assert len(window.records) == 1, "whitespace diagnostics ignored"


def test_query_timeout_preserves_chained_cause() -> None:
    """A deadline failure retains the transport exception for diagnosis."""
    cause = subprocess.TimeoutExpired(["journalctl"], 30)

    def timed_out(_argv: object) -> typ.NoReturn:
        """Raise the journal transport deadline failure."""
        raise cause

    with pytest.raises(MetricsQueryError, match="cannot execute") as caught:
        query(runner=timed_out)
    assert str(caught.value) == "cannot execute the user-journal query", (
        "timeout message"
    )
    assert caught.value.__cause__ is cause, "timeout cause retained"


@pytest.mark.parametrize("last_line", ["rejected", "malformed"])
def test_raw_overflow_precedes_decoding_and_source_filter(last_line: str) -> None:
    """Rejected records and malformed overflow still count against the raw cap."""
    rejected = FIXTURE.read_text().splitlines()[0]
    tail = rejected if last_line == "rejected" else "not-json"
    with pytest.raises(MetricsQueryError, match="exceeded its record cap") as caught:
        query(
            max_records=1,
            source="adapter",
            runner=journal_runner(rejected + "\n" + tail),
        )
    assert str(caught.value) == "journal query exceeded its record cap", "raw cap first"


def test_nonblank_malformed_line_keeps_its_original_whitespace() -> None:
    """Blank-line removal leaves the failing JSON input available unchanged."""
    line = "  not-json \t"
    with pytest.raises(MetricsQueryError, match="malformed JSON") as caught:
        query(runner=journal_runner("\n \n" + line + "\n\n"))
    cause = caught.value.__cause__
    assert isinstance(cause, json.JSONDecodeError), "JSON failure retains its cause"
    assert cause.doc == line, "nonblank raw line preserved verbatim"


@pytest.mark.parametrize("event", sorted(EVENTS))
@pytest.mark.parametrize("source", ["supervisor", "adapter"])
def test_event_source_matrix(event: str, source: str) -> None:
    """Every catalogue event belongs to exactly one of the two record sources."""
    raw = json.loads(FIXTURE.read_text().splitlines()[0])
    raw.update(BUILD_LIMITS_SOURCE=source, BUILD_LIMITS_EVENT=event)
    window = query(runner=journal_runner(json.dumps(raw)))
    owner = "adapter" if event == "adapter_finished" else "supervisor"
    expected = [(source, event)] if source == owner else []
    actual = [(record.source, record.event) for record in window.records]
    assert actual == expected, "event source restriction"


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"_SYSTEMD_USER_UNIT": "other.service"},
        {"_SYSTEMD_USER_UNIT": "buildlimits-supervisor.service.extra"},
        {"_SYSTEMD_USER_UNIT": None},
    ],
)
def test_supervisor_requires_exact_unit(overrides: dict[str, object]) -> None:
    """Missing, null and incorrect journal units cannot authenticate a supervisor."""
    raw = json.loads(FIXTURE.read_text().splitlines()[0])
    raw.pop("_SYSTEMD_USER_UNIT")
    raw.update(overrides)
    assert not query(runner=journal_runner(json.dumps(raw))).records, (
        "fixed unit required"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"_SYSTEMD_USER_UNIT": "buildlimits-supervisor.service"},
        {"_SYSTEMD_USER_UNIT": "other.service"},
    ],
)
def test_adapter_summary_is_self_reported(
    overrides: dict[str, object],
) -> None:
    """Adapter summaries are accepted independently of journal unit metadata."""
    raw = json.loads(FIXTURE.read_text().splitlines()[0])
    raw.pop("_SYSTEMD_USER_UNIT")
    raw.update(BUILD_LIMITS_SOURCE="adapter", BUILD_LIMITS_EVENT="adapter_finished")
    raw.update(overrides)
    window = query(runner=journal_runner(json.dumps(raw)))
    assert [record.source for record in window.records] == ["adapter"], (
        "adapter accepted"
    )
    result = typ.cast("dict[str, typ.Any]", report(window))
    assert result["adapters"]["self_reported"] is True, "adapter trust remains explicit"


@pytest.mark.parametrize("name", ["source", "event", "schema"])
@pytest.mark.parametrize(
    ("is_missing", "value"),
    [
        pytest.param(True, None, id="missing"),
        pytest.param(False, "unknown", id="unknown"),
        pytest.param(False, "2", id="other-schema"),
        pytest.param(False, "", id="empty"),
        pytest.param(False, None, id="null"),
        pytest.param(False, 1, id="integer"),
        pytest.param(False, 1.0, id="float"),
        pytest.param(False, True, id="boolean"),
        pytest.param(False, ["supervisor"], id="source-array"),
        pytest.param(False, ["supervisor_started"], id="event-array"),
        pytest.param(False, ["1"], id="schema-array"),
        pytest.param(False, {"source": "supervisor"}, id="source-object"),
        pytest.param(False, {"event": "supervisor_started"}, id="event-object"),
        pytest.param(False, {"schema": "1"}, id="schema-object"),
    ],
)
def test_invalid_record_identity(name: str, is_missing: bool, value: object) -> None:
    """Source, event and schema identities reject missing or malformed values."""
    raw = json.loads(FIXTURE.read_text().splitlines()[0])
    field = f"BUILD_LIMITS_{name.upper()}"
    if is_missing:
        raw.pop(field)
    else:
        raw[field] = value
    window = query(runner=journal_runner(json.dumps(raw)))
    assert not window.records, "invalid record identity rejected"
