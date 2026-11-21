"""Age boundaries in the archive planner.

The cutoff is applied to the observation span a sidecar supplies, and it is
inclusive: a row observed exactly at the cutoff is still history. These tests
pin the whole-segment deletion, the suffix trim and the cases where an unknown
span or a zero bound leaves a segment alone.
"""

import datetime as dt

from archive_plan_helpers import NOW, archived, entry, plan, policy

from buildlimits.outcome_archive_trim import Trim
from buildlimits.outcome_segment_inventory import Inventory


def test_an_archive_older_than_the_cutoff_is_deleted() -> None:
    """A segment whose last row predates the cutoff is wholly expired."""
    old = archived(
        1,
        span=(NOW - dt.timedelta(days=2), NOW - dt.timedelta(days=1, seconds=1)),
    )
    boundary = archived(2, span=(NOW - dt.timedelta(days=1), NOW))
    fresh = archived(3, span=(NOW, NOW))

    decision = plan(Inventory(archived=(old, boundary, fresh)), policy(max_age_days=1))

    assert [item.sequence for item in decision.deletes] == [1], (
        "only the archive strictly older than the cutoff is deleted"
    )


def test_a_record_at_the_cutoff_is_retained() -> None:
    """The boundary is inclusive: exactly thirty days old is still history."""
    boundary = archived(
        1,
        span=(NOW - dt.timedelta(days=30), NOW - dt.timedelta(days=30)),
    )

    decision = plan(Inventory(archived=(boundary,)), policy(max_age_days=30))

    assert not decision.deletes, "the last row at the cutoff survives"
    assert not decision.trims, "the first row at the cutoff needs no trim"


def test_an_archive_straddling_the_cutoff_is_trimmed() -> None:
    """A segment holding retained rows keeps only its suffix after the cutoff."""
    straddling = archived(1, span=(NOW - dt.timedelta(days=2), NOW))

    decision = plan(Inventory(archived=(straddling,)), policy(max_age_days=1))

    assert not decision.deletes, "the segment holds retained rows"
    assert decision.trims == ((straddling, Trim(before=NOW - dt.timedelta(days=1))),), (
        "the trim drops the prefix strictly before the cutoff"
    )


def test_an_expired_sealed_source_is_deleted_whole() -> None:
    """Age eviction reaches a source the archiver has not compressed yet."""
    expired = entry(
        1,
        state="sealed",
        span=(NOW - dt.timedelta(days=2), NOW - dt.timedelta(days=1, seconds=1)),
    )

    decision = plan(Inventory(sealed=(expired,)), policy(max_age_days=1))

    assert [item.sequence for item in decision.deletes] == [1], (
        "the wholly expired source is evicted"
    )
    assert not decision.archive, "an expired source is not compressed first"


def test_a_sealed_source_straddling_the_cutoff_is_archived_untrimmed() -> None:
    """A source keeping rows is archived so the follow-up plan can trim it."""
    straddling = entry(
        1,
        state="sealed",
        span=(NOW - dt.timedelta(days=2), NOW),
    )

    decision = plan(Inventory(sealed=(straddling,)), policy(max_age_days=1))

    assert not decision.deletes, "the source keeps rows inside the cutoff"
    assert not decision.trims, "an uncompressed source cannot be rewritten"
    assert [item.sequence for item in decision.archive] == [1], (
        "it is archived for the follow-up trim"
    )


def test_an_entry_without_metadata_is_never_age_evicted() -> None:
    """Unknown observation times must not be guessed into a deletion."""
    unknown = archived(1, span=None)

    decision = plan(Inventory(archived=(unknown,)), policy(max_age_days=1))

    assert not decision.deletes, "no count or span means no age decision"
    assert not decision.trims, "no span means no trim"


def test_zero_max_age_disables_age_eviction() -> None:
    """A zero age bound keeps history whatever its dates."""
    ancient = archived(
        1,
        span=(NOW - dt.timedelta(days=9999), NOW - dt.timedelta(days=9999)),
    )

    decision = plan(Inventory(archived=(ancient,)), policy(max_age_days=0))

    assert decision.cutoff is None, "no cutoff is applied"
    assert not decision.deletes, "nothing is evicted by age"
    assert not decision.trims, "nothing is trimmed by age"
