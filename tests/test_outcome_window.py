"""Bounded immutable launch accounting for statistics windows."""

import pytest
from outcome_test_helpers import outcome_module

pytestmark = pytest.mark.xfail(
    strict=True, reason="EP-M3 red tests precede the outcome window implementation"
)


def test_window_bound_overflow_and_ignored_following_launches() -> None:
    """Retain 64 payloads, then reject growth after the 65th launch."""
    module = outcome_module("outcome_window")
    window = module.OutcomeWindow()

    for payload in range(64):
        window = window.append(payload)

    assert window.entries == tuple(range(64)), "all eligible payloads retain order"
    assert not window.is_overflowed, "exactly 64 launches remain eligible"

    overflowed = window.append(64)
    after_more = overflowed.append(65)

    assert overflowed.is_overflowed, "the first excess launch marks the window"
    assert len(overflowed.entries) == 64, "overflow preserves the hard payload bound"
    assert after_more == overflowed, "an overflowed window ignores further launches"
