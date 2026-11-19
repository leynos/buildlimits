"""Planning decisions for one archive run.

The planner is pure and decides every mutation before the archiver touches a
file, so these tests can state the age and byte boundaries exactly: the record
observed at the cutoff, the archive that just fits the budget and the trim
allowance that a later run can prove from a sidecar.
"""

import datetime as dt
import typing as typ
from pathlib import Path

from buildlimits.outcome_archive_plan import Plan, PlanSummary, plan_run
from buildlimits.outcome_archive_verify import Trim
from buildlimits.outcome_retention import RetentionPolicy
from buildlimits.outcome_segment_inventory import Inventory, SegmentEntry
from buildlimits.outcome_segments import segment_name

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)


def _policy(
    *,
    max_age_days: int = 30,
    max_bytes: int = 0,
    max_records: int | None = None,
) -> RetentionPolicy:
    """Build one policy with eviction bounds chosen by the test.

    Returns
    -------
    RetentionPolicy
        The policy, with a one-byte segment and headroom.
    """
    return _bounded(max_age_days, max_bytes, max_records)


def _bounded(
    max_age_days: int, max_bytes: int, max_records: int | None
) -> RetentionPolicy:
    """Return a policy with one-byte segment and headroom bounds.

    Returns
    -------
    RetentionPolicy
        The policy, with no other bound set.
    """
    return RetentionPolicy(
        max_age=dt.timedelta(days=max_age_days),
        max_bytes=max_bytes,
        max_records=max_records,
        headroom_bytes=1,
        segment_bytes=1,
    )


def _entry(
    sequence: int,
    *,
    state: typ.Literal["sealed", "archived"] = "archived",
    logical_bytes: int = 100,
    first: dt.datetime | None = None,
    last: dt.datetime | None = None,
    has_metadata: bool = True,
) -> SegmentEntry:
    """Build one inventory entry without touching a filesystem.

    Returns
    -------
    SegmentEntry
        An entry describing the requested state.
    """
    return _segment(sequence, state, logical_bytes, first, last, has_metadata)


def _segment(
    sequence: int,
    state: typ.Literal["sealed", "archived"],
    logical_bytes: int,
    first: dt.datetime | None,
    last: dt.datetime | None,
    has_metadata: bool,
) -> SegmentEntry:
    """Return an entry whose metadata fields follow ``has_metadata``.

    Returns
    -------
    SegmentEntry
        The assembled entry.
    """
    return SegmentEntry(
        sequence=sequence,
        path=Path("/state") / segment_name(sequence),
        state=state,
        physical_bytes=40,
        logical_bytes=logical_bytes,
        record_count=1 if has_metadata else None,
        first_observed_at=first,
        last_observed_at=last,
        salt_id="salt-1" if has_metadata else None,
        has_metadata=has_metadata,
    )


def _archived(
    sequence: int,
    *,
    logical_bytes: int = 100,
    first: dt.datetime | None = None,
    last: dt.datetime | None = None,
    has_metadata: bool = True,
) -> SegmentEntry:
    """Build one archived entry.

    Returns
    -------
    SegmentEntry
        The entry.
    """
    return _entry(
        sequence,
        state="archived",
        logical_bytes=logical_bytes,
        first=first,
        last=last,
        has_metadata=has_metadata,
    )


def _sealed(sequence: int, logical_bytes: int = 100) -> SegmentEntry:
    """Build one sealed source entry.

    Returns
    -------
    SegmentEntry
        The entry.
    """
    return _entry(sequence, state="sealed", logical_bytes=logical_bytes)


def _plan(
    inventory: Inventory,
    policy: RetentionPolicy,
    *,
    active_bytes: int = 0,
) -> Plan:
    """Plan one run at a fixed wall time.

    Returns
    -------
    Plan
        The planner's decision.
    """
    return plan_run(inventory, policy, now=NOW, active_bytes=active_bytes)


def test_an_empty_inventory_retains_only_active_bytes() -> None:
    """With no closed history, the active file is the whole estimate."""
    plan = _plan(Inventory(), _policy(), active_bytes=7)

    assert not plan.archive, "nothing is sealed"
    assert plan.retained_bytes == 7, "the estimate is the active size"
    assert plan.cutoff == NOW - dt.timedelta(days=30), "the age cutoff is applied"


def test_every_sealed_source_is_archived() -> None:
    """Sealed sources with no archive are the run's compression work."""
    inventory = Inventory(sealed=(_sealed(2, 20), _sealed(1, 10)))

    plan = _plan(inventory, _policy(), active_bytes=5)

    assert [entry.sequence for entry in plan.archive] == [2, 1], (
        "the inventory's own order is preserved"
    )
    assert plan.retained_bytes == 35, "sealed and active bytes are both retained"


def test_a_duplicate_is_reconciled_rather_than_archived() -> None:
    """A source beside its archive is settled by verification, not rework."""
    sealed = _sealed(1, 100)
    archived = _archived(1, logical_bytes=1000)
    inventory = Inventory(sealed=(sealed,), archived=(archived,))

    plan = _plan(inventory, _policy())

    assert plan.reconcile == ((sealed, archived),), "the pair is reconciled"
    assert not plan.archive, "the duplicated source is not archived again"
    assert plan.retained_bytes == 100, (
        "the source size is the conservative estimate for a matched pair"
    )


def test_damaged_sidecars_are_healed_except_for_duplicates() -> None:
    """Healing skips a sequence reconciliation already owns."""
    inventory = Inventory(
        sealed=(_sealed(1),),
        archived=(_archived(1), _archived(2)),
        missing_sidecars=(1, 2),
        corrupt_sidecars=(3,),
    )

    plan = _plan(inventory, _policy())

    assert [entry.sequence for entry in plan.heal] == [2], (
        "only the archive with no source is healed"
    )


def test_orphan_sidecars_are_carried_into_the_plan() -> None:
    """A sidecar with no archive is reported for quarantine."""
    plan = _plan(Inventory(orphan_sidecars=(4, 5)), _policy())

    assert plan.orphans == (4, 5), "the sequences are passed through unchanged"


def test_an_archive_older_than_the_cutoff_is_deleted() -> None:
    """A segment whose last row predates the cutoff is wholly expired."""
    old = _archived(
        1,
        first=NOW - dt.timedelta(days=2),
        last=NOW - dt.timedelta(days=1, seconds=1),
    )
    boundary = _archived(2, first=NOW - dt.timedelta(days=1), last=NOW)
    fresh = _archived(3, first=NOW, last=NOW)

    plan = _plan(Inventory(archived=(old, boundary, fresh)), _policy(max_age_days=1))

    assert [entry.sequence for entry in plan.deletes] == [1], (
        "only the archive strictly older than the cutoff is deleted"
    )


def test_a_record_at_the_cutoff_is_retained() -> None:
    """The boundary is inclusive: exactly thirty days old is still history."""
    boundary = _archived(
        1,
        first=NOW - dt.timedelta(days=30),
        last=NOW - dt.timedelta(days=30),
    )

    plan = _plan(Inventory(archived=(boundary,)), _policy(max_age_days=30))

    assert not plan.deletes, "the last row at the cutoff survives"
    assert not plan.trims, "the first row at the cutoff needs no trim"


def test_an_archive_straddling_the_cutoff_is_trimmed() -> None:
    """A segment holding retained rows keeps only its suffix after the cutoff."""
    straddling = _archived(1, first=NOW - dt.timedelta(days=2), last=NOW)

    plan = _plan(Inventory(archived=(straddling,)), _policy(max_age_days=1))

    assert not plan.deletes, "the segment holds retained rows"
    assert plan.trims == ((straddling, Trim(before=NOW - dt.timedelta(days=1))),), (
        "the trim drops the prefix strictly before the cutoff"
    )


def test_an_entry_without_metadata_is_never_age_evicted() -> None:
    """Unknown observation times must not be guessed into a deletion."""
    unknown = _archived(1, has_metadata=False)

    plan = _plan(Inventory(archived=(unknown,)), _policy(max_age_days=1))

    assert not plan.deletes, "no count or span means no age decision"
    assert not plan.trims, "no span means no trim"


def test_zero_max_age_disables_age_eviction() -> None:
    """A zero age bound keeps history whatever its dates."""
    ancient = _archived(
        1, first=NOW - dt.timedelta(days=9999), last=NOW - dt.timedelta(days=9999)
    )

    plan = _plan(Inventory(archived=(ancient,)), _policy(max_age_days=0))

    assert plan.cutoff is None, "no cutoff is applied"
    assert not plan.deletes, "nothing is evicted by age"
    assert not plan.trims, "nothing is trimmed by age"


def test_a_budget_already_met_plans_no_byte_work() -> None:
    """Retained bytes equal to the budget are not an excess."""
    first = _archived(1)
    second = _archived(2)

    plan = _plan(Inventory(archived=(first, second)), _policy(max_bytes=200))

    assert not plan.deletes, "an exact budget is satisfied"
    assert not plan.trims, "no rewrite is needed"
    assert plan.retained_bytes == 200, "the estimate is the budget"


def test_the_oldest_archive_is_deleted_first() -> None:
    """Whole archives are removed oldest-first until the remainder fits."""
    first = _archived(1)
    second = _archived(2)

    plan = _plan(Inventory(archived=(first, second)), _policy(max_bytes=50))

    assert [entry.sequence for entry in plan.deletes] == [1], (
        "the oldest archive is evicted whole"
    )
    assert [(entry.sequence, trim.allowed_bytes) for entry, trim in plan.trims] == [
        (2, 50)
    ], "the remaining archive is trimmed to the exact budget"
    assert plan.retained_bytes == 50, "the estimate reaches the budget"


def test_a_deletion_that_meets_the_budget_plans_no_rewrite() -> None:
    """Evicting one whole archive must not churn the next one in vain."""
    first = _archived(1, logical_bytes=100)
    second = _archived(2, logical_bytes=100)

    plan = _plan(Inventory(archived=(first, second)), _policy(max_bytes=100))

    assert [entry.sequence for entry in plan.deletes] == [1], (
        "the oldest archive alone reaches the budget"
    )
    assert not plan.trims, "the remaining archive is left untouched"
    assert plan.retained_bytes == 100, "the estimate is the exact budget"


def test_a_partial_archive_is_trimmed_to_the_excess() -> None:
    """When one archive covers the excess, it keeps the bytes that fit."""
    first = _archived(1, logical_bytes=100)
    second = _archived(2, logical_bytes=100)

    plan = _plan(Inventory(archived=(first, second)), _policy(max_bytes=150))

    assert not plan.deletes, "no archive is small enough to remove first"
    assert plan.trims == ((first, Trim(allowed_bytes=50, source_bytes=100)),), (
        "the oldest archive is cut to the remaining allowance"
    )
    assert plan.retained_bytes == 150, "the estimate is the exact budget"


def test_a_byte_trim_keeps_the_age_trim_it_composes_with() -> None:
    """One rewrite can apply both an age cutoff and a byte allowance."""
    straddling = _archived(
        1,
        logical_bytes=1000,
        first=NOW - dt.timedelta(days=2),
        last=NOW,
    )

    plan = _plan(
        Inventory(archived=(straddling,)),
        _policy(max_age_days=1, max_bytes=400),
    )

    ((entry, trim),) = plan.trims

    assert entry is straddling, "the same archive is rewritten once"
    assert trim.before == NOW - dt.timedelta(days=1), "the age cutoff survives"
    assert trim.allowed_bytes == 400, "the byte allowance is added"
    assert trim.source_bytes == 1000, "the allowance is measured against the source"


def test_a_sealed_source_does_not_hide_a_duplicate_from_the_estimate() -> None:
    """A duplicated sequence contributes exactly one retained size."""
    inventory = Inventory(
        sealed=(_sealed(1, 100),),
        archived=(_archived(1, logical_bytes=900),),
    )

    plan = _plan(inventory, _policy(max_bytes=0))

    assert plan.retained_bytes == 100, "the sequence is counted once"


def test_no_byte_limit_disables_byte_eviction() -> None:
    """A zero budget means unlimited retention, not an immediate wipe."""
    entries = tuple(
        _archived(sequence, logical_bytes=10**9) for sequence in range(1, 4)
    )

    plan = _plan(Inventory(archived=entries), _policy(max_bytes=0))

    assert not plan.deletes, "nothing is evicted by bytes"
    assert not plan.trims, "nothing is trimmed by bytes"


def test_a_record_cap_never_evicts_a_segment() -> None:
    """The explicit record cap bounds the writer, not archived history."""
    entries = tuple(_archived(sequence) for sequence in range(1, 4))

    plan = _plan(Inventory(archived=entries), _policy(max_bytes=0, max_records=1))

    assert not plan.deletes, "a record cap cannot pick a row to drop"
    assert plan.retained_bytes == 300, "every segment stays retained"


def test_the_summary_counts_every_operation(snapshot: SnapshotAssertion) -> None:
    """A summary describes a plan without naming a path or a sequence."""
    plan = Plan(
        reconcile=((_sealed(1), _archived(1)),),
        archive=(_sealed(2),),
        heal=(_archived(3),),
        orphans=(4,),
        trims=((_archived(5), Trim()),),
        deletes=(_archived(6),),
        retained_bytes=70,
        cutoff=None,
    )

    summary = PlanSummary.of(plan)

    assert (summary.reconcile, summary.archive, summary.heal) == (1, 1, 1), (
        "reconcile, archive and heal are counted"
    )
    assert (summary.orphan, summary.trim, summary.delete) == (1, 1, 1), (
        "orphan, trim and delete are counted"
    )
    assert summary.as_dict() == snapshot, "the JSON fields are closed and stable"
