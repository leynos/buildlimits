"""Logical-byte boundaries in the archive planner.

The budget counts logical uncompressed bytes, and the planner removes whole
archives oldest-first before it trims a partial one. A byte trim is deferred
until an age trim has been measured, because the allowance only fits the bytes
that survive the age rule.
"""

import datetime as dt

from archive_plan_helpers import NOW, archived, plan, policy, sealed

from buildlimits.outcome_archive_trim import Trim
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


def test_a_byte_trim_waits_until_the_age_trim_is_measured() -> None:
    """A byte allowance is not planned from an untrimmed straddler.

    The allowance fits the bytes that survive the age rule, so planning it
    from the source the age trim has not yet rewritten would overstate the
    excess and drop rows the policy allows. The age trim is planned alone and
    the follow-up plan measures the archive it leaves behind.
    """
    straddling = archived(
        1,
        logical_bytes=1000,
        span=(NOW - dt.timedelta(days=2), NOW),
    )

    decision = plan(
        Inventory(archived=(straddling,)),
        policy(max_age_days=1, max_bytes=400),
    )

    assert decision.trims == ((straddling, Trim(before=NOW - dt.timedelta(days=1))),), (
        "the age trim is planned without a byte allowance"
    )
    assert decision.retained_bytes == 1000, (
        "the estimate keeps the bytes the untrimmed sidecar proves"
    )


def test_a_deleted_archive_is_never_also_trimmed() -> None:
    """A record cap may delete an archive the byte plan already trimmed.

    The byte walk trims the oldest archive to half the budget, and the record
    walk then finds that archive fits the record excess whole. Rewriting a
    file that is about to be removed would only churn it, so the deletion wins
    and no trim is published.
    """
    first = archived(1, logical_bytes=100)
    second = archived(2, logical_bytes=100)

    decision = plan(
        Inventory(archived=(first, second)),
        policy(max_bytes=150, max_records=1),
    )

    assert [item.sequence for item in decision.deletes] == [1], (
        "the byte-trimmed archive is removed whole"
    )
    assert not decision.trims, "a deleted archive is never also rewritten"
    assert decision.retained_bytes == 100, "only the newer archive remains"


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
