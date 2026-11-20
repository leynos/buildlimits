"""Logical-byte boundaries in the archive planner.

The budget counts logical uncompressed bytes, and the planner removes whole
archives oldest-first before it trims a partial one. A trim composes with an
age cutoff so one rewrite can apply both bounds.
"""

import datetime as dt

from archive_plan_helpers import NOW, archived, plan, policy, sealed

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


def test_a_deleted_archive_is_never_also_trimmed() -> None:
    """Byte planning may delete an archive the age plan already trimmed.

    Rewriting a file that is about to be removed would only churn it, so the
    deletion wins and the surviving archive keeps its own trim.
    """
    straddling = tuple(
        archived(
            sequence,
            logical_bytes=100,
            span=(NOW - dt.timedelta(days=2), NOW),
        )
        for sequence in (1, 2)
    )

    decision = plan(
        Inventory(archived=straddling),
        policy(max_age_days=1, max_bytes=100),
    )

    assert [item.sequence for item in decision.deletes] == [1], (
        "the oldest archive is removed whole"
    )
    assert [(item.sequence, trim.before) for item, trim in decision.trims] == [
        (2, NOW - dt.timedelta(days=1))
    ], "only the surviving archive is rewritten"


def test_a_byte_budget_evicts_a_sealed_source_before_a_newer_archive() -> None:
    """A source awaiting archival is evicted whole when it is the oldest."""
    source = sealed(1, logical_bytes=100)
    archive = archived(2, logical_bytes=100)

    decision = plan(
        Inventory(sealed=(source,), archived=(archive,)),
        policy(max_bytes=100),
    )

    assert [(item.sequence, item.state) for item in decision.deletes] == [
        (1, "sealed")
    ], "the oldest entry is removed whole"
    assert not decision.trims, "nothing is rewritten"
    assert not decision.archive, "an evicted source is not compressed first"
    assert decision.retained_bytes == 100, "the newer archive alone remains"


def test_a_sealed_source_that_straddles_the_budget_is_not_trimmed() -> None:
    """An uncompressed source cannot be trimmed, so the walk stops there."""
    source = sealed(1, logical_bytes=100)
    archive = archived(2, logical_bytes=100)

    decision = plan(
        Inventory(sealed=(source,), archived=(archive,)),
        policy(max_bytes=150),
    )

    assert not decision.deletes, "the oldest entry is too large to evict whole"
    assert not decision.trims, "a sealed source is never given a trim"
    assert [item.sequence for item in decision.archive] == [1], (
        "the source is archived so the follow-up plan can trim it"
    )


def test_no_byte_limit_disables_byte_eviction() -> None:
    """A zero budget means unlimited retention, not an immediate wipe."""
    entries = tuple(archived(sequence, logical_bytes=10**9) for sequence in range(1, 4))

    decision = plan(Inventory(archived=entries), policy(max_bytes=0))

    assert not decision.deletes, "nothing is evicted by bytes"
    assert not decision.trims, "nothing is trimmed by bytes"
