"""Publish one archive run's outcome through the bounded metrics catalogue.

The report is the operator's answer for one firing; the metric is what a
dashboard or an alert can see over many. Both come from the same closed
vocabularies, and the event names no path and carries no exception text.
"""

import collections
import typing as typ

if typ.TYPE_CHECKING:
    from buildlimits.metrics import MetricsPort
    from buildlimits.outcome_archive_report import RunReport

__all__ = ["publish_run"]


def publish_run(metrics: MetricsPort | None, report: RunReport) -> None:
    """Publish one ``archive_run`` event, when a caller supplied a port.

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
    metrics.emit(
        "archive_run",
        archive_status=report.status,
        archive_reason=report.reason or "none",
        archived=counts["archived"],
        trimmed=counts["trimmed"],
        deleted=counts["deleted"],
        quarantined=counts["quarantined"],
        failures=len(report.failures),
        runtime_seconds=elapsed,
    )
