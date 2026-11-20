"""The planner's decisions beyond age and byte eviction.

A plan settles half-published pairs before anything else, names every orphan
sidecar for quarantine, and summarizes itself without naming a path or a
sequence. These tests pin those decisions; the age and byte boundaries live in
the sibling age and byte modules.
"""

import dataclasses as dc
import datetime as dt
import typing as typ

import pytest
from archive_plan_helpers import NOW, archived, plan, policy, sealed

from buildlimits.outcome_archive_plan import Plan, PlanSummary
from buildlimits.outcome_archive_trim import Trim
from buildlimits.outcome_segment_inventory import Inventory

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion


def test_an_empty_inventory_retains_only_active_bytes() -> None:
    """With no closed history, the active file is the whole estimate."""
    decision = plan(Inventory(), policy(), active_bytes=7)

    assert not decision.archive, "nothing is sealed"
    assert decision.retained_bytes == 7, "the estimate is the active size"
    assert decision.cutoff == NOW - dt.timedelta(days=30), "the age cutoff is applied"


def test_every_sealed_source_is_archived() -> None:
    """Sealed sources with no archive are the run's compression work."""
    inventory = Inventory(sealed=(sealed(2, 20), sealed(1, 10)))

    decision = plan(inventory, policy(), active_bytes=5)

    assert [item.sequence for item in decision.archive] == [2, 1], (
        "the inventory's own order is preserved"
    )
    assert decision.retained_bytes == 35, "sealed and active bytes are both retained"


def test_a_duplicate_is_reconciled_rather_than_archived() -> None:
    """A source beside its archive is settled by verification, not rework."""
    source = sealed(1, 100)
    archive = archived(1, logical_bytes=1000)
    inventory = Inventory(sealed=(source,), archived=(archive,))

    decision = plan(inventory, policy())

    assert decision.reconcile == ((source, archive),), "the pair is reconciled"
    assert not decision.archive, "the duplicated source is not archived again"
    assert decision.retained_bytes == 100, (
        "the source size is the conservative estimate for a matched pair"
    )


def test_a_reconciled_duplicate_defers_a_configured_budget() -> None:
    """A cap is not decided while a duplicated sequence is excluded.

    Reconciliation settles the pair in the same run, so the budget is decided
    by the follow-up plan instead of from a total that leaves the duplicated
    history out.
    """
    inventory = Inventory(sealed=(sealed(1),), archived=(archived(1), archived(2)))

    decision = plan(inventory, policy(max_records=1))

    assert decision.retention_deferred is True, "the cap is left to the follow-up"
    assert not decision.trims and not decision.deletes, "nothing is evicted yet"


def test_an_unmeasured_archive_defers_a_configured_byte_budget() -> None:
    """An archive counted at its file size leaves the byte budget undecided."""
    inventory = Inventory(archived=(archived(1, span=None),), missing_sidecars=(1,))

    decision = plan(inventory, policy(max_bytes=10))

    assert [item.sequence for item in decision.heal] == [1], "the sidecar is rebuilt"
    assert decision.retention_deferred is True, "the byte walk waits for the heal"


def test_a_duplicate_without_a_budget_defers_nothing() -> None:
    """A plan with no bound to enforce has no budget decision to defer."""
    inventory = Inventory(sealed=(sealed(1),), archived=(archived(1), archived(2)))

    decision = plan(inventory, policy())

    assert decision.retention_deferred is False, "no configured budget was skipped"


def test_damaged_sidecars_are_healed_except_for_duplicates() -> None:
    """Healing skips a sequence reconciliation already owns."""
    inventory = Inventory(
        sealed=(sealed(1),),
        archived=(archived(1), archived(2)),
        missing_sidecars=(1, 2),
        corrupt_sidecars=(3,),
    )

    decision = plan(inventory, policy())

    assert [item.sequence for item in decision.heal] == [2], (
        "only the archive with no source is healed"
    )


def test_orphan_sidecars_are_carried_into_the_plan() -> None:
    """A sidecar with no archive is reported for quarantine."""
    decision = plan(Inventory(orphan_sidecars=(4, 5)), policy())

    assert decision.orphans == (4, 5), "the sequences are passed through unchanged"


def test_a_sealed_source_does_not_hide_a_duplicate_from_the_estimate() -> None:
    """A duplicated sequence contributes exactly one retained size."""
    inventory = Inventory(
        sealed=(sealed(1, 100),),
        archived=(archived(1, logical_bytes=900),),
    )

    decision = plan(inventory, policy(max_bytes=0))

    assert decision.retained_bytes == 100, "the sequence is counted once"


@dc.dataclass(frozen=True, slots=True)
class _CapCase:
    """One record-cap scenario and the plan it must produce.

    Attributes
    ----------
    cap : int
        The policy's record bound.
    archives : int
        How many archived entries the inventory holds.
    rows : int
        How many rows each archive holds.
    active_records : int
        How many rows the writer's active file still holds.
    deletes : tuple[int, ...]
        The sequences of the archives the plan must delete, oldest first.
    trims : tuple[tuple[int, int], ...]
        The archive sequence and the number of rows it must trim.
    """

    cap: int
    archives: int
    rows: int
    active_records: int
    deletes: tuple[int, ...]
    trims: tuple[tuple[int, int], ...]


_CAP_CASES = [
    pytest.param(
        _CapCase(cap=1, archives=3, rows=1, active_records=0, deletes=(1, 2), trims=()),
        id="whole-archives-oldest-first",
    ),
    pytest.param(
        _CapCase(
            cap=4, archives=3, rows=3, active_records=0, deletes=(1,), trims=((2, 2),)
        ),
        id="straddling-archive-trimmed",
    ),
    pytest.param(
        _CapCase(
            cap=10, archives=4, rows=2, active_records=6, deletes=(1, 2), trims=()
        ),
        id="active-rows-consume-the-cap",
    ),
    pytest.param(
        _CapCase(cap=5, archives=2, rows=1, active_records=6, deletes=(1, 2), trims=()),
        id="active-rows-exceed-the-cap",
    ),
]


@pytest.mark.parametrize("case", _CAP_CASES)
def test_a_record_cap_bounds_retained_history(case: _CapCase) -> None:
    """The cap counts the rows the writer still holds as well as closed rows.

    Whole archives are deleted oldest first. An archive that straddles the
    remaining excess keeps only its newest rows, while a cap the active rows
    already exceed empties the closed history: the archiver refuses to rewrite
    the writer's file, so the excess waits for the writer's next compaction.
    """
    entries = tuple(
        dc.replace(archived(sequence), record_count=case.rows)
        for sequence in range(1, case.archives + 1)
    )

    decision = plan(
        Inventory(archived=entries),
        policy(max_bytes=0, max_records=case.cap),
        active_records=case.active_records,
    )

    assert tuple(item.sequence for item in decision.deletes) == case.deletes, (
        "whole archives are deleted oldest first"
    )
    assert (
        tuple((item.sequence, trim.records) for item, trim in decision.trims)
        == case.trims
    ), "the archive straddling the cap keeps only its newest rows"


def test_a_record_cap_counts_the_writers_unsealed_rows() -> None:
    """The cap bounds the whole store, so the active file's rows count too.

    Counting only archives would let a store hold the cap in archived history
    and another cap in the file the writer is still filling.
    """
    entries = tuple(archived(sequence) for sequence in range(1, 4))

    decision = plan(
        Inventory(archived=entries),
        policy(max_bytes=0, max_records=4),
        active_records=2,
    )

    assert [item.sequence for item in decision.deletes] == [1], (
        "the active rows are part of the excess the plan must remove"
    )


def test_a_record_cap_evicts_the_oldest_sealed_source_first() -> None:
    """A sealed row counts towards the cap and is evictable like an archive.

    Counting the sealed row but evicting only archives would delete the newer
    archive and keep the older row, which is the reverse of oldest-first
    retention.
    """
    source = sealed(1)
    archive = archived(2)

    decision = plan(
        Inventory(sealed=(source,), archived=(archive,)),
        policy(max_bytes=0, max_records=1),
    )

    assert [(item.sequence, item.state) for item in decision.deletes] == [
        (1, "sealed")
    ], "the oldest entry is evicted whole"
    assert not decision.archive, "an evicted source is not compressed first"
    assert decision.retained_bytes == 100, "the newer archive alone remains"


def test_a_record_cap_stops_at_an_entry_with_no_known_count() -> None:
    """An unknown count ends the walk instead of evicting newer history."""
    unknown = dc.replace(sealed(1), record_count=None)

    decision = plan(
        Inventory(sealed=(unknown,), archived=(archived(2), archived(3))),
        policy(max_bytes=0, max_records=1),
    )

    assert not decision.deletes, "no newer entry is evicted ahead of the unknown one"


def test_the_summary_counts_every_operation(snapshot: SnapshotAssertion) -> None:
    """A summary describes a plan without naming a path or a sequence."""
    decision = Plan(
        reconcile=((sealed(1), archived(1)),),
        archive=(sealed(2),),
        heal=(archived(3),),
        orphans=(4,),
        trims=((archived(5), Trim()),),
        deletes=(archived(6),),
        retained_bytes=70,
        cutoff=None,
        retention_deferred=True,
    )

    summary = PlanSummary.of(decision)

    assert (summary.reconcile, summary.archive, summary.heal) == (1, 1, 1), (
        "reconcile, archive and heal are counted"
    )
    assert (summary.orphan, summary.trim, summary.delete) == (1, 1, 1), (
        "orphan, trim and delete are counted"
    )
    # The summary passes the plan's measurement through unchanged; asserting
    # that here keeps the byte value out of the snapshot, which pins the closed
    # set of field names without restating a number the plan supplied. The
    # serialized value is asserted before it is redacted, because a wrong JSON
    # value would otherwise be masked by the placeholder.
    assert summary.retained_bytes == 70, "the plan's measurement is reported"
    reported = summary.as_dict()
    assert reported["retained_logical_bytes"] == 70, (
        "the serialized summary carries the plan's measurement"
    )
    reported["retained_logical_bytes"] = "<bytes>"
    assert reported == snapshot, "the JSON fields are closed and stable"
