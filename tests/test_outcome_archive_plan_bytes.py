"""Logical-byte boundaries in the archive planner.

The budget counts logical uncompressed bytes, and the planner removes whole
archives oldest-first before it trims a partial one. A trim composes with an
age cutoff so one rewrite can apply both bounds.
"""

import datetime as dt

from archive_plan_helpers import NOW, archived, plan, policy

from buildlimits.outcome_archive_verify import Trim
from buildlimits.outcome_segment_inventory import Inventory


def test_a_budget_already_met_plans_no_byte_work() -> None:
    """Retained bytes equal to the budget are not an excess."""
    first = archived(1)
    second = archived(2)

    decision = plan(Inventory(archived=(first, second)), policy(max_bytes=200))

    assert not decision.deletes, "an exact budget is satisfied"
    assert not decision.trims, "no rewrite is needed"
    assert decision.retained_bytes == 200, "the estimate is the budget"


def test_the_oldest_archive_is_deleted_first() -> None:
    """Whole archives are removed oldest-first until the remainder fits."""
    first = archived(1)
    second = archived(2)

    decision = plan(Inventory(archived=(first, second)), policy(max_bytes=50))

    assert [item.sequence for item in decision.deletes] == [1], (
        "the oldest archive is evicted whole"
    )
    assert [(item.sequence, trim.allowed_bytes) for item, trim in decision.trims] == [
        (2, 50)
    ], "the remaining archive is trimmed to the exact budget"
    assert decision.retained_bytes == 50, "the estimate reaches the budget"


def test_a_deletion_that_meets_the_budget_plans_no_rewrite() -> None:
    """Evicting one whole archive must not churn the next one in vain."""
    first = archived(1, logical_bytes=100)
    second = archived(2, logical_bytes=100)

    decision = plan(Inventory(archived=(first, second)), policy(max_bytes=100))

    assert [item.sequence for item in decision.deletes] == [1], (
        "the oldest archive alone reaches the budget"
    )
    assert not decision.trims, "the remaining archive is left untouched"
    assert decision.retained_bytes == 100, "the estimate is the exact budget"


def test_a_partial_archive_is_trimmed_to_the_excess() -> None:
    """When one archive covers the excess, it keeps the bytes that fit."""
    first = archived(1, logical_bytes=100)
    second = archived(2, logical_bytes=100)

    decision = plan(Inventory(archived=(first, second)), policy(max_bytes=150))

    assert not decision.deletes, "no archive is small enough to remove first"
    assert decision.trims == ((first, Trim(allowed_bytes=50, source_bytes=100)),), (
        "the oldest archive is cut to the remaining allowance"
    )
    assert decision.retained_bytes == 150, "the estimate is the exact budget"


def test_a_byte_trim_keeps_the_age_trim_it_composes_with() -> None:
    """One rewrite can apply both an age cutoff and a byte allowance."""
    straddling = archived(
        1,
        logical_bytes=1000,
        span=(NOW - dt.timedelta(days=2), NOW),
    )

    decision = plan(
        Inventory(archived=(straddling,)),
        policy(max_age_days=1, max_bytes=400),
    )

    ((item, trim),) = decision.trims

    assert item is straddling, "the same archive is rewritten once"
    assert trim.before == NOW - dt.timedelta(days=1), "the age cutoff survives"
    assert trim.allowed_bytes == 400, "the byte allowance is added"
    assert trim.source_bytes == 1000, "the allowance is measured against the source"


def test_no_byte_limit_disables_byte_eviction() -> None:
    """A zero budget means unlimited retention, not an immediate wipe."""
    entries = tuple(archived(sequence, logical_bytes=10**9) for sequence in range(1, 4))

    decision = plan(Inventory(archived=entries), policy(max_bytes=0))

    assert not decision.deletes, "nothing is evicted by bytes"
    assert not decision.trims, "nothing is trimmed by bytes"
