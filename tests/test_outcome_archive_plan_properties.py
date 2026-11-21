"""Planner invariants over generated measured histories.

The retention walks are the one place the byte budget and the record cap decide
which history survives. These properties hold a measured inventory to the
contract the ExecPlan states for every plan: eviction is a whole-entry
oldest-first prefix, at most one straddler is trimmed, and the estimate left
after the plan fits the configured bound. The boundary modules pin each edge of
those rules; these properties pin the ordering and fit across sizes.
"""

import dataclasses as dc
import datetime as dt
import typing as typ

from archive_plan_helpers import NOW, archived, plan, policy
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


def _deleted_prefix(decision: Plan) -> list[int]:
    """Return the sequences the plan deletes, requiring an oldest-first prefix.

    Returns
    -------
    list[int]
        The deleted sequences, oldest first and without a gap.
    """
    deleted = [entry.sequence for entry in decision.deletes]
    assert deleted == list(range(1, len(deleted) + 1)), (
        "whole entries are evicted oldest-first, with no gap"
    )
    return deleted


def _assert_oldest_first(
    entries: tuple[SegmentEntry, ...], decision: Plan
) -> tuple[SegmentEntry, ...]:
    """Assert the eviction shape and return the surviving entries.

    Returns
    -------
    tuple[SegmentEntry, ...]
        The entries no deletion removes, oldest first.
    """
    deleted = _deleted_prefix(decision)
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


def _counted(counts: list[int]) -> tuple[SegmentEntry, ...]:
    """Build one measured archive per generated row count.

    Returns
    -------
    tuple[SegmentEntry, ...]
        Archives whose sidecars report the given counts, oldest first.
    """
    return tuple(
        dc.replace(entry, record_count=count)
        for entry, count in zip(
            _measured_archives([100] * len(counts)), counts, strict=True
        )
    )


def _cap_plan(entries: tuple[SegmentEntry, ...], cap: int, active: int = 0) -> Plan:
    """Plan one record cap over the given measured archives.

    Returns
    -------
    Plan
        The record-cap decision, counting the active rows the cap also counts.
    """
    return plan(
        Inventory(archived=entries),
        policy(max_age_days=0, max_bytes=0, max_records=cap),
        active_records=active,
    )


def _assert_record_cap(
    entries: tuple[SegmentEntry, ...],
    decision: Plan,
    cap: int,
    active: int,
) -> None:
    """Assert the plan meets the record cap and counts the active rows.

    Parameters
    ----------
    entries : tuple[SegmentEntry, ...]
        The measured inventory the plan decided.
    decision : Plan
        The record-cap plan.
    cap : int
        The configured record cap.
    active : int
        The rows the active file holds, which the cap also counts.
    """
    survivors = _assert_oldest_first(entries, decision)
    trim = _trim_of(decision)
    kept = sum(entry.record_count or 0 for entry in survivors)
    kept = kept if trim is None else kept - trim.records
    # The plan must keep every closed row the cap can afford. Checking only
    # that the result fits the cap would accept a plan that deleted history it
    # did not need to, and an active-only excess still leaves nothing to keep.
    total = sum(entry.record_count or 0 for entry in entries)
    assert kept == min(total, max(cap - active, 0)), (
        "the plan retains the closed rows the cap can afford"
    )
    assert decision.retention_deferred == (kept + active > cap), (
        "the cap is deferred exactly when the plan cannot meet it"
    )
    if decision.retention_deferred:
        assert not survivors, "an exhausted closed history is what defers a cap"
        return
    if trim is None:
        assert kept + active <= cap, "a settled plan meets the cap"
        return
    assert trim.records < (survivors[0].record_count or 0), (
        "the straddler keeps some rows and needs no whole deletion"
    )
    assert kept + active == cap, "the straddler is capped at what remains"


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
    # The walk stops as soon as the budget is met, so the plan keeps every byte
    # it can afford: deleting more than min(sum, budget) would discard history
    # the settled store keeps, which the surviving-sum check alone accepts.
    assert decision.retained_bytes == min(sum(sizes), budget), (
        "the plan retains the history the budget can afford"
    )
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
    allowance = trim.allowed_bytes
    assert allowance is not None, "a byte trim records its allowance"
    assert decision.retained_bytes == (
        sum(entry.logical_bytes for entry in survivors[1:]) + allowance
    ), "the estimate composes the surviving sum with the trim allowance"


@given(
    counts=st.lists(st.integers(min_value=1, max_value=5), max_size=8),
    cap=st.integers(min_value=0, max_value=20),
)
@settings(max_examples=50, deadline=None)
def test_a_planned_record_cap_fits_every_generated_history(
    counts: list[int], cap: int
) -> None:
    """A measured record cap is met without evicting newer history first."""
    entries = _counted(counts)

    decision = _cap_plan(entries, cap)

    _assert_record_cap(entries, decision, cap, active=0)


@given(
    spans=st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=6),  # days the last row predates NOW
            st.integers(min_value=0, max_value=4),  # days between first and last row
        ),
        max_size=8,
    ).map(lambda pairs: sorted(pairs, reverse=True))
)
@settings(max_examples=50, deadline=None)
def test_age_retention_removes_an_expired_prefix(
    spans: list[tuple[int, int]],
) -> None:
    """Age eviction deletes an expired prefix and trims only the boundary.

    Sequences are ordered oldest-first, as a store's are, so the property also
    pins that a fresh entry is never rewritten while an expired one survives.
    """
    cutoff = NOW - dt.timedelta(days=1)
    entries = tuple(
        archived(
            sequence,
            span=(
                NOW - dt.timedelta(days=end + width),
                NOW - dt.timedelta(days=end),
            ),
        )
        for sequence, (end, width) in enumerate(spans, start=1)
    )

    decision = plan(Inventory(archived=entries), policy(max_age_days=1))

    deleted = _deleted_prefix(decision)
    survivors = tuple(entry for entry in entries if entry.sequence > len(deleted))
    for sequence in deleted:
        last = entries[sequence - 1].last_observed_at
        assert last is not None, "the generated history is fully measured"
        assert last < cutoff, "only a wholly expired entry is deleted"
    straddlers = set()
    for entry in survivors:
        first = entry.first_observed_at
        last = entry.last_observed_at
        assert first is not None and last is not None, (
            "the generated history is fully measured"
        )
        assert last >= cutoff, "a row at the cutoff is still history"
        if first < cutoff:
            straddlers.add(entry.sequence)
    trims = {entry.sequence: trim for entry, trim in decision.trims}
    assert set(trims) == straddlers, (
        "every surviving entry holding expired rows is rewritten"
    )
    for sequence, trim in trims.items():
        assert trim.before == cutoff, "the trim drops rows strictly before the cutoff"
        assert sequence not in deleted, "a deleted entry is never rewritten"
    assert not decision.retention_deferred, "a measured history decides its deadline"


@given(
    counts=st.lists(st.integers(min_value=1, max_value=6), max_size=6),
    active=st.integers(min_value=0, max_value=20),
    cap=st.integers(min_value=0, max_value=20),
)
@settings(max_examples=50, deadline=None)
def test_a_record_cap_counts_the_unsealed_active_rows(
    counts: list[int], active: int, cap: int
) -> None:
    """The cap counts the writer's rows, so an active-only excess evicts all."""
    entries = _counted(counts)

    decision = _cap_plan(entries, cap, active)

    _assert_record_cap(entries, decision, cap, active)
