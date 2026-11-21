"""The retention policy refuses a segment size the store cannot compact.

A legacy store compacts in place under ``MAX_COMPACT_BYTES``. A policy that
lets the active file grow past that bound would make every compaction fail, so
the policy is rejected when it is validated rather than at the first write.
"""

import datetime as dt

import pytest

from buildlimits.outcome_record_encode import encode_line
from buildlimits.outcome_record_format import MAX_COMPACT_BYTES, StoreHeader
from buildlimits.outcome_retention import (
    RetentionPolicy,
    header_floor_bytes,
    validate_policy,
)


def test_a_segment_at_the_compaction_bound_is_accepted() -> None:
    """The largest document a compaction can write is a usable segment size."""
    policy = RetentionPolicy(
        segment_bytes=MAX_COMPACT_BYTES, headroom_bytes=MAX_COMPACT_BYTES
    )

    validate_policy(policy)


def test_a_segment_larger_than_the_compaction_bound_is_refused() -> None:
    """A segment no in-place compaction could write is not a usable policy."""
    policy = RetentionPolicy(
        segment_bytes=MAX_COMPACT_BYTES + 1, headroom_bytes=MAX_COMPACT_BYTES + 1
    )

    with pytest.raises(ValueError, match="largest compactable"):
        validate_policy(policy)


def test_a_byte_budget_below_one_header_is_refused() -> None:
    """Compaction always writes a header, so a smaller budget is unreachable."""
    policy = RetentionPolicy(max_bytes=1, segment_bytes=1, headroom_bytes=1)

    with pytest.raises(ValueError, match="replacement store header"):
        validate_policy(policy)


def test_a_byte_budget_that_holds_a_header_is_accepted() -> None:
    """A budget above the header floor is a policy compaction can satisfy."""
    policy = RetentionPolicy(max_bytes=4096, segment_bytes=1, headroom_bytes=1)

    validate_policy(policy)


def test_a_byte_budget_at_the_header_floor_is_accepted() -> None:
    """The floor itself holds the longest replacement header compaction writes.

    The encoder preserves fractional seconds, so a header written with any
    microsecond value is longer than one written at a whole second. A budget
    one byte below this floor could not hold it, which
    ``test_a_byte_budget_below_one_header_is_refused`` pins from the other side.
    """
    policy = RetentionPolicy(
        max_bytes=header_floor_bytes(), segment_bytes=1, headroom_bytes=1
    )

    validate_policy(policy)


def test_the_header_floor_holds_an_independently_encoded_fractional_header() -> None:
    """The floor is the longest header the encoder writes, plus its newline.

    Encoding the header here rather than reusing ``header_floor_bytes`` keeps
    the boundary honest: a floor that reverted to a whole-second header would
    be smaller than this document and fail the first assertion.
    """
    spoken_at = dt.datetime(1970, 1, 1, microsecond=1, tzinfo=dt.UTC)
    fractional = encode_line(StoreHeader("0" * 64, spoken_at)).encode() + b"\n"
    whole_second = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
    whole = encode_line(StoreHeader("0" * 64, whole_second)).encode() + b"\n"

    assert header_floor_bytes() == len(fractional), (
        "the floor is exactly one fractional-second header with its newline"
    )
    assert len(whole) < header_floor_bytes(), (
        "a whole-second header is shorter, so it cannot be the floor"
    )


def test_a_byte_budget_one_below_the_header_floor_is_refused() -> None:
    """The boundary is exact: one byte less than the maximal header fails."""
    policy = RetentionPolicy(
        max_bytes=header_floor_bytes() - 1, segment_bytes=1, headroom_bytes=1
    )

    with pytest.raises(ValueError, match="replacement store header"):
        validate_policy(policy)


def test_a_zero_byte_budget_means_unlimited() -> None:
    """Zero is the documented "no byte bound", not an impossible budget."""
    policy = RetentionPolicy(max_bytes=0, segment_bytes=1, headroom_bytes=1)

    validate_policy(policy)
