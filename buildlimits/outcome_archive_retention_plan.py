"""Age, byte and record eviction decided over one closed-history inventory.

The planner's other decisions describe what to archive, repair or quarantine;
this module decides what retention removes. Keeping the bounds here means the
order the report documents — age by observation time, then bytes and records by
append order — has one implementation for both maintenance operations.

A retention-only run may rewrite a sealed straddler in place, so the same walk
plans either form. An archive run leaves a sealed straddler to the follow-up
plan that runs after it has been compressed.
"""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_archive_age import age_trims, plan_age_step
from buildlimits.outcome_archive_budget import (
    Budget,
    budget_totals_are_unsettled,
    plan_byte_steps,
    plan_record_steps,
    require_measurable_active,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_archive_trim import Trim
    from buildlimits.outcome_segment_inventory import Inventory, SegmentEntry

__all__ = ["Retention", "retention_steps"]


@dc.dataclass(frozen=True, slots=True)
class Retention:
    """The eviction one plan decided, with the cause of each deletion.

    Attributes
    ----------
    deletes : tuple[SegmentEntry, ...]
        Entries to remove whole, oldest first.
    trims : tuple[tuple[SegmentEntry, Trim], ...]
        Entries to rewrite with a prefix trim.
    causes : tuple[tuple[int, str], ...]
        The closed bound that made each deletion eligible, sorted by sequence.
    deferred : bool
        Whether a configured budget was left to the follow-up plan.
    """

    deletes: tuple[SegmentEntry, ...]
    trims: tuple[tuple[SegmentEntry, Trim], ...]
    causes: tuple[tuple[int, str], ...]
    deferred: bool


def retention_steps(
    inventory: Inventory,
    budget: Budget,
    duplicated: set[int],
) -> Retention:
    """Decide age, byte and record eviction over closed history, oldest first.

    Sealed sources are candidates as well as archives, and both count towards
    the budgets: excluding a sealed source would make the plan delete newer
    archives while older history survived. An archive-mode run only ever
    removes a sealed source whole, and the follow-up plan trims it once this
    run has archived it. A retention-only run rewrites it in place instead, so
    a straddler does not have to be compressed first.

    The byte and record walks run only when every surviving entry has been
    measured, the active file's surviving rows were read, and no duplicated
    sequence is excluded from the candidates; otherwise their totals misstate
    what the store retains, and a whole deletion chosen against them could
    remove an older entry the settled store keeps. Age eviction is decided from
    the cutoff alone, so it is planned either way and the follow-up pass
    re-plans the budgets. A walk that stops at an entry it cannot yet settle
    defers its budget the same way.

    Each bound is applied in turn and the deletions it plans are attributed to
    it, so the report names the bound that actually removed an entry. Age runs
    first, then bytes, then the record cap: an entry the age plan already
    removed keeps ``age`` even if a later bound would also have removed it.

    Returns
    -------
    Retention
        The deletions, trims, their causes, and whether a configured budget
        was left to the follow-up plan.
    """
    decisions = _Decisions(
        candidates=tuple(
            entry for entry in inventory.effective if entry.sequence not in duplicated
        )
    )
    _age_steps(decisions, budget.cutoff)
    decisions.trims = age_trims(
        decisions.candidates,
        budget.cutoff,
        decisions.deletes,
        include_sealed=budget.can_trim_sealed,
    )
    measurable = _budgets_are_measurable(
        decisions.candidates, budget, decisions.deletes, duplicated
    )
    settled = _capacity_steps(decisions, budget, measurable=measurable)
    # A later walk may delete an archive an earlier one already planned to trim;
    # rewriting a file that is about to be removed would only churn it, so a
    # deleted sequence is never also planned as a trim.
    trimmed = tuple(
        (entry, decisions.trims[entry.sequence])
        for entry in decisions.candidates
        if entry.sequence in decisions.trims and entry.sequence not in decisions.deletes
    )
    bounded = budget.policy.has_byte_limit or budget.policy.max_records is not None
    deferred = bounded and (not measurable or not settled)
    return Retention(
        deletes=tuple(
            decisions.deletes[sequence] for sequence in sorted(decisions.deletes)
        ),
        trims=trimmed,
        causes=tuple(sorted(decisions.causes.items())),
        deferred=deferred,
    )


@dc.dataclass(slots=True)
class _Decisions:
    """The eviction one retention plan accumulates in place.

    Attributes
    ----------
    candidates : tuple[SegmentEntry, ...]
        The closed entries the plan may act on, oldest first.
    deletes : dict[int, SegmentEntry]
        The entries planned for whole deletion, by sequence.
    causes : dict[int, str]
        The closed bound that made each deletion eligible, by sequence.
    trims : dict[int, Trim]
        The trims planned so far, by sequence. The byte and record walks
        compose with them.
    """

    candidates: tuple[SegmentEntry, ...]
    deletes: dict[int, SegmentEntry] = dc.field(default_factory=dict)
    causes: dict[int, str] = dc.field(default_factory=dict)
    trims: dict[int, Trim] = dc.field(default_factory=dict)


def _age_steps(decisions: _Decisions, cutoff: dt.datetime | None) -> None:
    """Apply the age rule and attribute its whole deletions."""
    if cutoff is not None:
        for entry in decisions.candidates:
            plan_age_step(entry, cutoff, decisions.deletes)
    _attribute(decisions.deletes, decisions.causes, "age")


def _capacity_steps(decisions: _Decisions, budget: Budget, *, measurable: bool) -> bool:
    """Apply the byte and record bounds, or report why they were deferred.

    Returns
    -------
    bool
        Whether the walks settled both bounds.
    """
    if not measurable:
        # An unmeasurable cap is a run invariant rather than a planning
        # decision: even a pass that defers its walk has to report it.
        require_measurable_active(budget)
        return False
    settled = plan_byte_steps(
        decisions.candidates, budget, decisions.deletes, decisions.trims
    )
    _attribute(decisions.deletes, decisions.causes, "bytes")
    settled = (
        plan_record_steps(
            decisions.candidates, budget, decisions.deletes, decisions.trims
        )
        and settled
    )
    _attribute(decisions.deletes, decisions.causes, "records")
    return settled


def _attribute(
    deletes: dict[int, SegmentEntry], causes: dict[int, str], cause: str
) -> None:
    """Attribute every deletion not yet explained to the bound just applied."""
    for sequence in deletes:
        causes.setdefault(sequence, cause)


def _budgets_are_measurable(
    candidates: tuple[SegmentEntry, ...],
    budget: Budget,
    deletes: cabc.Mapping[int, SegmentEntry],
    duplicated: set[int],
) -> bool:
    """Whether a budget walk can safely choose whole deletions this pass.

    A duplicated sequence is left out of the candidates while reconciliation
    settles it, so its rows would be missing from the total the walk acts on.
    An active file whose rows could not be read leaves a byte budget without
    the committed bytes it must count; its whole-file size includes an append
    the writer may roll back, so it cannot stand in. ``_active_is_measurable``
    and ``budget_totals_are_unsettled`` report the other conditions: an entry
    whose span crosses the cutoff, or one with no trustworthy measurement. A
    later pass re-plans from the reconciled and measured history.

    Returns
    -------
    bool
        True when none of those conditions applies.
    """
    if duplicated:
        return False
    if not _active_is_measurable(budget):
        return False
    return not budget_totals_are_unsettled(candidates, budget.cutoff, deletes)


def _active_is_measurable(budget: Budget) -> bool:
    """Whether an unmeasured active file still lets the budgets be decided.

    An unmeasured file contributes only its whole size, and that size includes
    every byte of an append the writer has not committed; a failed append is
    truncated back out. Evicting closed history against those bytes would
    discard history the settled store keeps, so a configured byte bound is
    deferred until a later scan reads the file, whether or not a cutoff
    applies. The record cap needs no deferral here: a configured cap is
    refused outright by ``require_measurable_active``.

    Returns
    -------
    bool
        True when the active figures the configured byte bound needs were read.
    """
    if budget.active.has_metadata:
        return True
    return not budget.policy.has_byte_limit
