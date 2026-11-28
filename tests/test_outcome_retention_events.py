"""The retention operation's bounded metrics event.

A retention-only firing publishes its own event name with the same payload an
archive firing uses, so a dashboard can compare the two without learning a
second field set.
"""

import datetime as dt
import typing as typ
from pathlib import Path

from outcome_archive_helpers import NOW, layout, policy

from buildlimits.metrics import Metrics
from buildlimits.outcome_archive import RunOptions, run_retain

if typ.TYPE_CHECKING:
    from conftest import RecordingSink

_FINISHED = NOW + dt.timedelta(seconds=2)


def test_a_retention_run_publishes_its_own_event(
    tmp_path: Path, metric_sink: RecordingSink
) -> None:
    """The retention operation is measurable without being read as archival."""
    report = run_retain(
        layout(tmp_path),
        policy(),
        RunOptions(
            now=NOW,
            clock=lambda: _FINISHED,
            metrics=Metrics(metric_sink, source="adapter"),
        ),
    )

    (record,) = metric_sink.records
    assert record["BUILD_LIMITS_EVENT"] == "retention_run", (
        "the event names the operation that produced it"
    )
    assert record["BUILD_LIMITS_ARCHIVED"] == "0", (
        "a retention run measures its archival count as zero"
    )
    assert record["BUILD_LIMITS_ARCHIVE_STATUS"] == "ok", (
        "the shared payload carries the closed status"
    )
    assert report.operation == "retain", "and the report says the same"
