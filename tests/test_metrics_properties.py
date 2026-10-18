"""Generated scalar boundaries for identifier-free metric measurements."""

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from buildlimits.metrics import COUNTERS, DURATIONS, MAX_COUNTER, bounded_field


@given(
    name=st.sampled_from(sorted(DURATIONS)),
    value=st.one_of(
        st.integers(min_value=0),
        st.floats(min_value=0, allow_nan=False, allow_infinity=False),
    ),
)
@example(name="wait_seconds", value=1.2345)
@example(name="runtime_seconds", value=MAX_COUNTER + 1)
def test_duration_rounding_and_cap(name: str, value: int | float) -> None:
    """Finite durations retain millisecond precision and respect the hard cap."""
    encoded = bounded_field(name, value)
    assert encoded is not None, "non-negative finite durations are accepted"
    fraction = encoded.partition(".")[2]
    assert len(fraction) == 3 and fraction.isdecimal(), "three decimal places"
    assert float(encoded) == pytest.approx(
        round(min(value, MAX_COUNTER), 3), rel=0, abs=0
    ), "round the capped duration to milliseconds"


@given(name=st.sampled_from(sorted(COUNTERS)), value=st.integers(min_value=0))
@example(name="connection_attempts", value=MAX_COUNTER + 1)
def test_counter_cap(name: str, value: int) -> None:
    """Integral counters preserve small values and saturate above the cap."""
    encoded = bounded_field(name, value)
    assert encoded is not None, "non-negative integer counters are accepted"
    assert int(encoded) == min(value, MAX_COUNTER), "counters saturate without wrapping"


@given(
    name=st.sampled_from(sorted(COUNTERS)),
    value=st.one_of(
        st.booleans(), st.floats(), st.text(), st.none(), st.integers(max_value=-1)
    ),
)
def test_counter_rejects_non_counts(name: str, value: object) -> None:
    """Booleans, non-integers and negative values cannot masquerade as counts."""
    assert bounded_field(name, value) is None, "invalid counts are omitted"


@given(
    name=st.sampled_from(sorted(DURATIONS)),
    value=st.one_of(
        st.booleans(),
        st.integers(max_value=-1),
        st.floats(max_value=-5e-324, allow_nan=False, allow_infinity=False),
        st.sampled_from([float("nan"), float("inf"), float("-inf")]),
        st.text(),
        st.none(),
    ),
)
def test_duration_rejects_invalid_scalars(name: str, value: object) -> None:
    """Negative, non-finite and non-numeric durations never reach the journal."""
    assert bounded_field(name, value) is None, "invalid durations are omitted"
