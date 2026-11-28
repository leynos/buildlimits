"""Publish one archive run's outcome through the bounded metrics catalogue.

The report is the operator's answer for one firing; the metric is what a
dashboard or an alert can see over many. Both come from the same closed
vocabularies, and the event names no path and carries no exception text.
"""

import collections
import typing as typ

from buildlimits.outcome_archive_evictions import EVICTION_CAUSES

if typ.TYPE_CHECKING:
    from buildlimits.metrics import MetricsPort
    from buildlimits.outcome_archive_report import RunReport

__all__ = ["publish_run"]


def publish_run(metrics: MetricsPort | None, report: RunReport) -> None:
    """Publish one run event, named for the operation that produced it.

    An archive firing publishes ``archive_run``; a retention-only firing
    publishes ``retention_run``. Both carry the same fields, so one dashboard
    query can compare them, and ``archived`` stays a measured zero for a
    retention-only run rather than the field disappearing.

    Parameters
    ----------
    metrics : MetricsPort | None
        The validated event transport, or None when the caller collects no
        metrics.
    report : RunReport
        The completed run or inspection.

    Examples
    --------
    A run that archived one source publishes ``archived`` as one and every
    other action count as zero.
    """
    if metrics is None:
        return
    counts = collections.Counter(action.action for action in report.actions)
    elapsed = (report.finished_at - report.started_at).total_seconds()
    event = "retention_run" if report.operation == "retain" else "archive_run"
    metrics.emit(
        event,
        archive_status=report.status,
        archive_reason=report.reason or "none",
        archived=counts["archived"],
        trimmed=counts["trimmed"],
        deleted=counts["deleted"],
        quarantined=counts["quarantined"],
        failures=len(report.failures),
        runtime_seconds=elapsed,
        retained_logical_bytes=report.retained_bytes,
        retained_records=report.retained_records,
        retained_unmeasured_entries=report.unmeasured_entries,
        **{
            f"evicted_{cause}": count
            for cause, count in _archive_evictions(report).items()
        },
    )


def _archive_evictions(report: RunReport) -> dict[str, int]:
    """Count this run's trims and deletions under their closed causes.

    Returns
    -------
    dict[str, int]
        One count per closed eviction cause, including the causes that did
        not act, so a dashboard can compare two firings directly.
    """
    counts = dict.fromkeys(EVICTION_CAUSES, 0)
    for action in report.actions:
        cause = action.reason
        if action.action in {"trimmed", "deleted"} and cause in counts:
            counts[cause] += 1
    return counts
