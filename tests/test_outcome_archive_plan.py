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
from buildlimits.outcome_archive_verify import Trim
from buildlimits.outcome_retention_report import ActiveHistory
from buildlimits.outcome_segment_inventory import Inventory

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion


def test_an_empty_inventory_retains_only_active_bytes() -> None:
    """With no closed history, the active file is the whole estimate."""
    decision = plan(Inventory(), policy(), active=ActiveHistory(logical_bytes=7))

    assert not decision.archive, "nothing is sealed"
    assert decision.retained_bytes == 7, "the estimate is the active size"
    assert decision.cutoff == NOW - dt.timedelta(days=30), "the age cutoff is applied"


def test_every_sealed_source_is_archived() -> None:
    """Sealed sources with no archive are the run's compression work."""
    inventory = Inventory(sealed=(sealed(2, 20), sealed(1, 10)))

    decision = plan(inventory, policy(), active=ActiveHistory(logical_bytes=5))

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
        active=ActiveHistory(record_count=case.active_records),
    )

    assert tuple(item.sequence for item in decision.deletes) == case.deletes, (
        "whole archives are deleted oldest first"
    )
    assert (
        tuple((item.sequence, trim.records) for item, trim in decision.trims)
        == case.trims
    ), "the archive straddling the cap keeps only its newest rows"


def test_an_incomplete_active_measurement_contributes_no_rows_to_the_cap() -> None:
    """History is never evicted on rows the run could not count.

    An incomplete read reports its row count as a lower bound, so the planner
    treats it as zero and over-retains rather than deleting archives to meet
    a cap the unread rows may already exceed.
    """
    entries = tuple(
        dc.replace(archived(sequence), record_count=1) for sequence in (1, 2, 3)
    )

    decision = plan(
        Inventory(archived=entries),
        policy(max_bytes=0, max_records=6),
        active=ActiveHistory(record_count=6, complete=False, reason="corrupt"),
    )

    assert not decision.deletes, "an unread count does not evict history"
    assert not decision.trims, "nor does it trim history to meet the cap"


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
    )

    summary = PlanSummary.of(decision)

    assert (summary.reconcile, summary.archive, summary.heal) == (1, 1, 1), (
        "reconcile, archive and heal are counted"
    )
    assert (summary.orphan, summary.trim, summary.delete) == (1, 1, 1), (
        "orphan, trim and delete are counted"
    )
    assert summary.as_dict() == snapshot, "the JSON fields are closed and stable"
