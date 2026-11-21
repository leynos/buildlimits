"""Render one archive report as the stable JSON document it is published in.

The report model says what a run achieved. This adapter says how that answer
is versioned and rendered, so the model stays a plain value and the command
and the state writer cannot disagree about the persisted shape.
"""

import typing as typ

from buildlimits.utc_text import optional_utc_text, utc_text

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive_context import ActionResult
    from buildlimits.outcome_archive_report import RunReport

ARCHIVE_SCHEMA = 1

__all__ = ["ARCHIVE_SCHEMA", "action_dict", "report_dict"]


def report_dict(report: RunReport) -> dict[str, object]:
    """Return the stable JSON fields a report or state file carries.

    Parameters
    ----------
    report : RunReport
        The run or inspection result to render.

    Returns
    -------
    dict[str, object]
        A JSON-compatible mapping with no filesystem paths.

    Examples
    --------
    The document names its schema so an operator can tell one persisted
    shape from the next; ``report_dict(report)["schema"]`` is one here.
    """
    return {
        "schema": ARCHIVE_SCHEMA,
        "status": report.status,
        "dry_run": report.dry_run,
        "started_at": utc_text(report.started_at),
        "finished_at": utc_text(report.finished_at),
        "active_bytes": report.active_bytes,
        "retained_logical_bytes": report.retained_bytes,
        "retained_records": report.retained_records,
        "observed_from": optional_utc_text(report.observed_from),
        "observed_until": optional_utc_text(report.observed_until),
        "unmeasured_entries": report.unmeasured_entries,
        "budget_bytes": report.budget_bytes,
        "cutoff": optional_utc_text(report.cutoff),
        "plan": None if report.plan is None else report.plan.as_dict(),
        "actions": [action_dict(action) for action in report.actions],
        "failures": [
            {"sequence": sequence, "reason": reason}
            for sequence, reason in report.failures
        ],
        "gaps": list(report.gaps),
        "gaps_truncated": report.gaps_truncated,
        "missing_sidecars": list(report.missing_sidecars),
        "corrupt_sidecars": list(report.corrupt_sidecars),
        "orphan_sidecars": list(report.orphan_sidecars),
        "reason": report.reason,
    }


def action_dict(action: ActionResult) -> dict[str, object]:
    """Render one performed action for the report.

    Parameters
    ----------
    action : ActionResult
        One operation the run performed.

    Returns
    -------
    dict[str, object]
        The action's closed name, sequence, sizes and optional reason.
    """
    return {
        "action": action.action,
        "sequence": action.sequence,
        "logical_bytes": action.logical_bytes,
        "physical_bytes": action.physical_bytes,
        "reason": action.reason,
    }
