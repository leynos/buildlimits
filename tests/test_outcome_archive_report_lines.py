"""The plain report's timing lines, which a journal reader checks a run by."""

import datetime as dt
import typing as typ

from buildlimits.outcome_archive_cli import _write_report
from buildlimits.outcome_archive_report import RunReport
from buildlimits.utc_text import utc_text

if typ.TYPE_CHECKING:
    import pytest

_START = dt.datetime(2026, 11, 19, 1, 2, 3, tzinfo=dt.UTC)


def test_the_plain_report_names_its_start_and_elapsed_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A journal reader can see when a firing began and how long it took.

    The JSON document already carried both times; the plain form an operator
    reads from the journal did not, so a slow or stalled firing was invisible
    beside a status that looked normal.
    """
    report = RunReport(
        status="ok",
        started_at=_START,
        finished_at=_START + dt.timedelta(milliseconds=1250),
    )

    _write_report(report, as_json=False)

    output = capsys.readouterr().out
    assert f"started: {utc_text(_START)}" in output, "the start is the run's own"
    assert "elapsed: 1.250s" in output, "the duration is the report's span"
