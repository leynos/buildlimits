"""Planner invariants over generated measured histories.

The retention walks are the one place the byte budget and the record cap decide
which history survives. These properties hold a measured inventory to the
contract the ExecPlan states for every plan: eviction is a whole-entry
oldest-first prefix, at most one straddler is trimmed, and the estimate left
after the plan fits the configured bound. The boundary modules pin each edge of
those rules; these properties pin the ordering and fit across sizes.
"""

import dataclasses as dc
import typing as typ

from archive_plan_helpers import archived, plan, policy
from hypothesis import given, settings
from hypothesis import strategies as st

from buildlimits.outcome_segment_inventory import Inventory, SegmentEntry

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive_plan import Plan
    from buildlimits.outcome_archive_trim import Trim


def _measured_archives(sizes: list[int]) -> tuple[SegmentEntry, ...]:
    """Build one measured archive per size, in ascending sequence order.

    Returns
    -------
    tuple[SegmentEntry, ...]
        Archives with a trustworthy sidecar and no age span.
    """
    return tuple(
        archived(sequence, logical_bytes=size)
        for sequence, size in enumerate(sizes, start=1)
    )


def _assert_oldest_first(
    entries: tuple[SegmentEntry, ...], decision: Plan
) -> tuple[SegmentEntry, ...]:
    """Assert the eviction shape and return the surviving entries.

    Returns
    -------
    tuple[SegmentEntry, ...]
        The entries no deletion removes, oldest first.
    """
    deleted = [entry.sequence for entry in decision.deletes]
    assert deleted == list(range(1, len(deleted) + 1)), (
        "whole entries are deleted oldest-first, with no gap"
    )
    survivors = tuple(entry for entry in entries if entry.sequence > len(deleted))
    assert len(decision.trims) <= 1, "only the straddler is rewritten"
    if decision.trims:
        trimmed, _trim = decision.trims[0]
        assert survivors and trimmed is survivors[0], "the trim follows every deletion"
    return survivors


def _trim_of(decision: Plan) -> Trim | None:
    """Return the plan's single trim, when it has one.

    Returns
    -------
    Trim or None
        The planned trim, or None when nothing is rewritten.
    """
    return decision.trims[0][1] if decision.trims else None


@given(
    sizes=st.lists(st.integers(min_value=1, max_value=1_000), max_size=8),
    budget=st.integers(min_value=1, max_value=4_000),
)
@settings(max_examples=50, deadline=None)
def test_a_planned_byte_budget_fits_every_generated_history(
    sizes: list[int], budget: int
) -> None:
    """A measured byte budget is met without evicting newer history first."""
    entries = _measured_archives(sizes)

    decision = plan(
        Inventory(archived=entries),
        policy(max_age_days=0, max_bytes=budget),
    )

    survivors = _assert_oldest_first(entries, decision)
    assert not decision.retention_deferred, "a measured history decides its budget"
    assert decision.retained_bytes <= budget, "the planned history fits the budget"
    trim = _trim_of(decision)
    if trim is None:
        assert decision.retained_bytes == sum(
            entry.logical_bytes for entry in survivors
        ), "without a trim the estimate is the surviving sum"
        return
    assert survivors, "a trim rewrites an archive that survives"
    assert trim.source_bytes == survivors[0].logical_bytes, (
        "the allowance is measured against the archive it rewrites"
    )
    assert 0 < (trim.allowed_bytes or 0) < trim.source_bytes, (
        "the straddler keeps some bytes and needs no whole deletion"
    )
    assert decision.retained_bytes == budget, (
        "the straddler is trimmed to exactly the remaining allowance"
    )


@given(
    counts=st.lists(st.integers(min_value=1, max_value=5), max_size=8),
    cap=st.integers(min_value=0, max_value=20),
)
@settings(max_examples=50, deadline=None)
def test_a_planned_record_cap_fits_every_generated_history(
    counts: list[int], cap: int
) -> None:
    """A measured record cap is met without evicting newer history first."""
    entries = tuple(
        dc.replace(entry, record_count=count)
        for entry, count in zip(
            _measured_archives([100] * len(counts)), counts, strict=True
        )
    )

    decision = plan(
        Inventory(archived=entries),
        policy(max_age_days=0, max_bytes=0, max_records=cap),
    )

    survivors = _assert_oldest_first(entries, decision)
    assert not decision.retention_deferred, "a measured history decides its cap"
    retained = sum(entry.record_count or 0 for entry in survivors)
    trim = _trim_of(decision)
    kept = retained if trim is None else retained - trim.records
    assert kept <= cap, "the planned history fits the cap"
    if trim is not None:
        assert trim.records < (survivors[0].record_count or 0), (
            "the straddler keeps some rows and needs no whole deletion"
        )
        assert kept == cap, "the straddler is capped at exactly the remaining rows"
