"""The retention policy refuses a segment size the store cannot compact.

A legacy store compacts in place under ``MAX_COMPACT_BYTES``. A policy that
lets the active file grow past that bound would make every compaction fail, so
the policy is rejected when it is validated rather than at the first write.
"""

import pytest

from buildlimits.outcome_record_format import MAX_COMPACT_BYTES
from buildlimits.outcome_retention import RetentionPolicy, validate_policy


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
