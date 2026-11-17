"""Replay one recorded workload through both metrics producers.

The acceptance criterion for #56 is that the supervisor's reported maxima do
not get worse when per-record samples are replaced by periodic snapshots. The
fixture tests pin each window shape separately, which cannot show that. This
module records one workload of queue and held depths, renders it the way the
legacy producer did and the way the gauge accumulator does now, and compares
the maxima the report publishes for each window.

The workload is rendered with a forced snapshot at each recorded instant. That
is deliberately stronger than the production cadence: every peak the workload
contains reaches the journal, so a fold that dropped or delayed a peak would
show up as a smaller maximum rather than staying hidden inside a later
interval.
"""

import json
import typing as typ

from conftest import RecordingSink
from journal_fixtures import journal_runner

from buildlimits.metrics import Metrics
from buildlimits.metrics_pressure import PressureMetrics
from buildlimits.metrics_query import query
from buildlimits.metrics_report import report

if typ.TYPE_CHECKING:
    import collections.abc as cabc

BASE_TIMESTAMP_US = 1_767_225_600_000_000
SERVICE_UNIT = "buildlimits-supervisor.service"

# One recorded workload: monotonic seconds, queue depth, held tokens. The
# depths peak at different moments so a producer that folded only the final
# sample, or only one of the two counts, cannot pass.
WORKLOAD: tuple[tuple[float, int, int], ...] = (
    (0.0, 0, 0),
    (4.0, 3, 0),
    (9.0, 6, 1),
    (12.0, 2, 4),
    (20.0, 7, 3),
    (26.0, 1, 5),
    (33.0, 4, 2),
    (41.0, 0, 1),
)

type Maxima = tuple[float, float]


def _journal_lines(
    records: cabc.Iterable[tuple[dict[str, str], float]],
) -> str:
    """Render validated records as the JSON lines a journal query returns.

    Parameters
    ----------
    records : Iterable[tuple[dict[str, str], float]]
        Each validated record paired with the workload instant it describes.

    Returns
    -------
    str
        Newline-separated journal records with the supervisor's unit and a
        timestamp the report can order.
    """
    lines = []
    for record, at in records:
        envelope = {
            **record,
            "_SYSTEMD_USER_UNIT": SERVICE_UNIT,
            "__REALTIME_TIMESTAMP": str(BASE_TIMESTAMP_US + int(at * 1_000_000)),
        }
        lines.append(json.dumps(envelope))
    return "\n".join(lines)


def _instants() -> cabc.Iterator[float]:
    """Yield the workload's monotonic instants in the order they were recorded.

    Returns
    -------
    Iterator[float]
        One instant per workload sample.
    """
    return (at for at, _, _ in WORKLOAD)


def _aligned_lines(sink: RecordingSink) -> str:
    """Pair each accepted record with the workload instant it was read at.

    Returns
    -------
    str
        The journal lines for the records the sink accepted.
    """
    return _journal_lines(zip(sink.records, _instants(), strict=True))


def _legacy_window() -> str:
    """Render the workload as the per-record samples the old producer wrote.

    Returns
    -------
    str
        One accepted record per workload instant, each carrying its own queue
        and held sample.
    """
    sink = RecordingSink()
    emitter = Metrics(sink)
    for _at, queued, held in WORKLOAD:
        assert emitter.emit("request_admitted", queued=queued, held=held), (
            "the legacy producer wrote a sample on every record"
        )
    return _aligned_lines(sink)


def _snapshot_window() -> str:
    """Render the workload through the periodic gauge producer.

    Returns
    -------
    str
        The ``gauges`` records the accumulator publishes for the workload.
    """
    sink = RecordingSink()
    state: dict[str, object] = {}
    now = [0.0]
    observer = PressureMetrics(Metrics(sink), lambda: state, clock=lambda: now[0])
    for at, queued, held in WORKLOAD:
        state.update(queued=queued, held=held)
        now[0] = at
        observer.observe(at)
        assert observer.flush(at, force=True), "each recorded instant is published"
    assert all(record["BUILD_LIMITS_EVENT"] == "gauges" for record in sink.records), (
        "the snapshot window carries no per-record counter samples"
    )
    return _aligned_lines(sink)


def _maxima(window: str) -> Maxima:
    """Read the maxima the report publishes for one journal window.

    Returns
    -------
    Maxima
        The queue and held maxima.
    """
    result = typ.cast(
        "dict[str, typ.Any]", report(query(runner=journal_runner(window)))
    )
    gauges = typ.cast("dict[str, float]", result["gauges"])
    return gauges["queue_maximum"], gauges["held_maximum"]


def test_the_same_workload_reports_the_same_maxima_in_both_formats() -> None:
    """Periodic peaks preserve the maxima the per-record samples reported."""
    expected: Maxima = (
        float(max(queued for _, queued, _ in WORKLOAD)),
        float(max(held for _, _, held in WORKLOAD)),
    )
    legacy = _maxima(_legacy_window())
    snapshots = _maxima(_snapshot_window())
    assert legacy == expected, "the legacy window reports the workload's maxima"
    assert snapshots == expected, "the snapshot window reports the same maxima"
    assert snapshots == legacy, "the change must not lower or raise either maximum"
