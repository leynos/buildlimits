"""The report fields both settled-report builders derive from one store view.

An inspection and an executed run read the same inventory, active file and
policy, so they describe the same bytes, rows, gaps, limits and retained
history. Building those fields here keeps the shared surface in one place
without coupling the report value to either caller.
"""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_archive_history import (
    observed_history,
    physical_fallback_bytes,
    retained_logical_bytes,
)
from buildlimits.outcome_retention_history import retained_history
from buildlimits.outcome_retention_report import RetentionLimits

if typ.TYPE_CHECKING:
    import datetime as dt

    from buildlimits.outcome_archive_history import ActiveHistory
    from buildlimits.outcome_archive_plan import PlanSummary
    from buildlimits.outcome_archive_run import ArchiveRun
    from buildlimits.outcome_retention_report import RetainedHistory
    from buildlimits.outcome_segment_inventory import Inventory

__all__ = ["SettledFields", "settled_fields"]


class SettledFields(typ.TypedDict):
    """The report fields both builders derive from one settled pair."""

    active_bytes: int
    retained_bytes: int
    fallback_bytes: int
    retained_records: int
    observed_from: dt.datetime | None
    observed_until: dt.datetime | None
    unmeasured_entries: int
    budget_bytes: int
    cutoff: dt.datetime | None
    plan: PlanSummary | None
    gaps: tuple[int, ...]
    gaps_truncated: bool
    missing_sidecars: tuple[int, ...]
    corrupt_sidecars: tuple[int, ...]
    orphan_sidecars: tuple[int, ...]
    sealed_replacing: tuple[int, ...]
    limits: RetentionLimits
    retained: RetainedHistory


def settled_fields(
    run: ArchiveRun,
    inventory: Inventory,
    active: ActiveHistory,
    *,
    is_settled: bool = True,
) -> SettledFields:
    """Describe the fields both report builders derive from one settled pair.

    Returns
    -------
    SettledFields
    """
    observed = observed_history(inventory, active)
    retained = retained_history(inventory, active)
    if not is_settled:
        # The inventory and active file describe different versions of the
        # store, so the counts are a lower bound rather than a measurement.
        retained = dc.replace(retained, complete=False)
    return {
        "active_bytes": active.bytes,
        "retained_bytes": retained_logical_bytes(inventory, active),
        "fallback_bytes": physical_fallback_bytes(inventory),
        "retained_records": observed.records,
        "observed_from": observed.observed_from,
        "observed_until": observed.observed_until,
        "unmeasured_entries": observed.unmeasured,
        "budget_bytes": run.context.policy.max_bytes,
        "cutoff": run.plan.cutoff,
        "plan": run.summary,
        "gaps": inventory.gaps,
        "gaps_truncated": inventory.gaps_truncated,
        "missing_sidecars": inventory.missing_sidecars,
        "corrupt_sidecars": inventory.corrupt_sidecars,
        "orphan_sidecars": inventory.orphan_sidecars,
        "sealed_replacing": inventory.sealed_replacing,
        "limits": RetentionLimits.of(run.context.policy),
        "retained": retained,
    }
