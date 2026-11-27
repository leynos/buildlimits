"""The bounded summary of one archive plan.

A report names the operations a plan holds without naming a path or a
sequence, so the summary is a small value that the plan, the report and the
state writer can pass around without reaching back into the planner.
"""

import dataclasses as dc
import typing as typ

from buildlimits.utc_text import optional_utc_text

if typ.TYPE_CHECKING:
    import datetime as dt

    from buildlimits.outcome_archive_plan import Plan


@dc.dataclass(frozen=True, slots=True)
class PlanSummary:
    """Counts and estimates that describe a plan without naming paths.

    Attributes
    ----------
    reconcile : int
        Sources whose existing archive will be checked.
    archive : int
        Sources that will be compressed.
    heal : int
        Sidecars that will be rebuilt.
    sealed_repair : int
        Sealed sources whose metadata will be re-derived.
    stale_marker : int
        Sealed rewrite markers whose source is already gone.
    orphan : int
        Sidecars that will be quarantined.
    trim : int
        Entries that will be rewritten, sealed in place or archived under XZ.
    delete : int
        Archives and sealed sources that will be removed.
    retained_bytes : int
        Conservative logical bytes remaining after the plan.
    cutoff : datetime | None
        The age cutoff applied.
    retention_deferred : bool
        Whether a configured budget is decided by a follow-up plan, so the trim
        and delete counts are not final.
    """

    reconcile: int = 0
    archive: int = 0
    heal: int = 0
    sealed_repair: int = 0
    stale_marker: int = 0
    orphan: int = 0
    trim: int = 0
    delete: int = 0
    retained_bytes: int = 0
    cutoff: dt.datetime | None = None
    retention_deferred: bool = False

    @classmethod
    def of(cls, plan: Plan) -> PlanSummary:
        """Summarize one plan.

        Parameters
        ----------
        plan : Plan
            The plan to summarize.

        Returns
        -------
        PlanSummary
            The counts and estimates.
        """
        return cls(
            reconcile=len(plan.reconcile),
            archive=len(plan.archive),
            heal=len(plan.heal),
            sealed_repair=len(plan.sealed_repairs),
            stale_marker=len(plan.stale_sealed_markers),
            orphan=len(plan.orphans),
            trim=len(plan.trims),
            delete=len(plan.deletes),
            retained_bytes=plan.retained_bytes,
            cutoff=plan.cutoff,
            retention_deferred=plan.retention_deferred,
        )

    def as_dict(self) -> dict[str, object]:
        """Return the stable JSON fields for a report.

        Returns
        -------
        dict[str, object]
            A JSON-compatible mapping of counts and estimates.
        """
        return {
            "reconcile": self.reconcile,
            "archive": self.archive,
            "heal": self.heal,
            "sealed_repair": self.sealed_repair,
            "stale_marker": self.stale_marker,
            "orphan": self.orphan,
            "trim": self.trim,
            "delete": self.delete,
            "retained_logical_bytes": self.retained_bytes,
            "cutoff": optional_utc_text(self.cutoff),
            "retention_deferred": self.retention_deferred,
        }
