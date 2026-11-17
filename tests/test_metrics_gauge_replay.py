"""Replay recorded workloads through both metrics producers.

The acceptance criterion for #56 is that the supervisor's reported maxima do
not get worse when per-record samples are replaced by periodic snapshots. The
fixture tests pin each window shape separately, which cannot show that. This
module records workloads of queue and held depths, renders each one the way the
legacy producer did and the way the gauge accumulator does now, and compares
the maxima the report publishes for each window.

Two snapshot renderings matter. The first forces a snapshot at every recorded
instant, which shows the fold preserves every sample. The second leaves the
cadence to the accumulator, so a peak that rises and falls between two
published snapshots has to survive inside the interval peak instead.
"""

import typing as typ

from conftest import RecordingSink
from journal_fixtures import journal_lines, journal_runner

from buildlimits.metrics import Metrics
from buildlimits.metrics_pressure import GaugePublisher
from buildlimits.metrics_query import query
from buildlimits.metrics_report import report

if typ.TYPE_CHECKING:
    import collections.abc as cabc

type Maxima = tuple[float, float]
type Workload = tuple[tuple[float, int, int], ...]

# One recorded workload: monotonic seconds, queue depth, held tokens. The
# depths peak at different moments so a producer that folded only the final
# sample, or only one of the two counts, cannot pass.
WORKLOAD: Workload = (
    (0.0, 0, 0),
    (4.0, 3, 0),
    (9.0, 6, 1),
    (12.0, 2, 4),
    (20.0, 7, 3),
    (26.0, 1, 5),
    (33.0, 4, 2),
    (41.0, 0, 1),
)

# The same shape with both peaks landing between published snapshots. The queue
# peak arrives two seconds after a step-triggered record, inside the minimum
# spacing, and the held peak follows a second later. The heartbeat at forty
# seconds is the first record that may carry them, so only the interval peak
# keeps either one.
PEAK_BETWEEN_SNAPSHOTS: Workload = (
    (0.0, 0, 0),
    (6.0, 5, 1),
    (8.0, 9, 2),
    (9.0, 1, 6),
    (40.0, 0, 0),
)


def _instants(workload: Workload) -> cabc.Iterator[float]:
    """Yield the workload's monotonic instants in the order they were recorded.

    Returns
    -------
    Iterator[float]
        One instant per workload sample.
    """
    return (at for at, _, _ in workload)


def _aligned_lines(sink: RecordingSink, workload: Workload) -> str:
    """Pair each accepted record with the workload instant it was read at.

    Returns
    -------
    str
        The journal lines for the records the sink accepted.
    """
    return journal_lines(zip(sink.records, _instants(workload), strict=True))


def _legacy_window(workload: Workload) -> str:
    """Render the workload as the per-record samples the old producer wrote.

    Returns
    -------
    str
        One accepted record per workload instant, each carrying its own queue
        and held sample.
    """
    sink = RecordingSink()
    emitter = Metrics(sink)
    for _at, queued, held in workload:
        assert emitter.emit("request_admitted", queued=queued, held=held), (
            "the legacy producer wrote a sample on every record"
        )
    return _aligned_lines(sink, workload)


def _snapshot_window(
    workload: Workload, *, force: bool
) -> tuple[str, tuple[float, ...]]:
    """Render the workload through the accumulator.

    Parameters
    ----------
    workload : Workload
        The recorded instants and depths to replay.
    force : bool
        Whether every sample is published, or the accumulator's cadence
        decides, so a test can show which peaks fell between two records.

    Returns
    -------
    tuple[str, tuple[float, ...]]
        The ``gauges`` records and the instants they were published at.
    """
    sink = RecordingSink()
    state: dict[str, object] = {}
    observer = GaugePublisher(Metrics(sink), lambda: state, clock=lambda: 0.0)
    published: list[float] = []
    for at, queued, held in workload:
        state.update(queued=queued, held=held)
        observer.sample(at)
        if observer.publish(at, force=force).is_written:
            published.append(at)
    assert len(published) == len(sink.records), "one record per published instant"
    assert all(record["BUILD_LIMITS_EVENT"] == "gauges" for record in sink.records), (
        "the snapshot window carries no per-record counter samples"
    )
    return journal_lines(zip(sink.records, published, strict=True)), tuple(published)


def _expected_maxima(workload: Workload) -> Maxima:
    """Read the workload's true maxima from its own samples.

    Returns
    -------
    Maxima
        The largest queue depth and the largest held-token count recorded.
    """
    return (
        float(max(queued for _, queued, _ in workload)),
        float(max(held for _, _, held in workload)),
    )


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
    expected = _expected_maxima(WORKLOAD)
    legacy = _maxima(_legacy_window(WORKLOAD))
    window, published = _snapshot_window(WORKLOAD, force=True)
    assert published == tuple(_instants(WORKLOAD)), (
        "the forced window publishes every recorded instant"
    )
    snapshots = _maxima(window)
    assert legacy == expected, "the legacy window reports the workload's maxima"
    assert snapshots == expected, "the snapshot window reports the same maxima"
    assert snapshots == legacy, "the change must not lower or raise either maximum"


def test_a_peak_between_snapshots_still_raises_the_reported_maximum() -> None:
    """A depth that rose and fell inside an interval is not lost."""
    expected = _expected_maxima(PEAK_BETWEEN_SNAPSHOTS)
    window, published = _snapshot_window(PEAK_BETWEEN_SNAPSHOTS, force=False)
    assert published, "the run must publish at least a baseline"
    assert 8.0 not in published, "the queue peak falls inside an interval"
    assert 9.0 not in published, "the held peak falls inside an interval"
    assert _maxima(window) == expected, (
        "the interval peak carries both depths the boundaries did not see"
    )
    assert _maxima(window) == _maxima(_legacy_window(PEAK_BETWEEN_SNAPSHOTS)), (
        "the formats agree even when no boundary lands on the peak"
    )
