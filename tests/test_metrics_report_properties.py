"""Nearest-rank and ordering properties for bounded duration reports."""

import math

from hypothesis import example, given
from hypothesis import strategies as st

from buildlimits.metrics import MAX_COUNTER
from buildlimits.metrics_report import percentiles


@given(
    values=st.lists(
        st.one_of(
            st.floats(
                min_value=0,
                max_value=MAX_COUNTER,
                allow_nan=False,
                allow_infinity=False,
            ),
            st.sampled_from([0.0, 0.0005, 1.2344, 1.2345, 1.2346]),
        ),
        max_size=150,
    ),
    rotation=st.integers(min_value=0, max_value=150),
)
@example(values=[], rotation=0)
@example(values=[1.2344, 1.2345, 1.2346], rotation=1)
def test_percentiles_nearest_rank(values: list[float], rotation: int) -> None:
    """Reports select ranks from the empirical distribution, regardless of order."""
    actual = percentiles(values)
    assert actual["count"] == len(values), "every observation contributes once"
    ordered = sorted(values)
    for percentage in (50, 90, 99):
        rank = math.ceil(len(values) * percentage / 100)
        expected = round(ordered[rank - 1], 3) if values else None
        assert actual[f"p{percentage}"] == expected, "nearest-rank statistic"
    expected_maximum = round(max(values), 3) if values else None
    assert actual["maximum"] == expected_maximum, "rounded largest observation"
    permutation = values[rotation:] + values[:rotation]
    assert percentiles(permutation) == actual, "ordering cannot change the distribution"
    assert percentiles(list(reversed(values))) == actual, "reversal preserves ranks"


@given(
    value=st.sampled_from([0.0005, 1.2344, 1.2345, 1.2346]),
    copies=st.integers(min_value=1, max_value=150),
)
def test_duplicate_rounding_boundaries(value: float, copies: int) -> None:
    """Repeated rounding-boundary samples yield the same value at every rank."""
    actual = percentiles([value] * copies)
    assert actual["count"] == copies, "each duplicate contributes one sample"
    for name in ("p50", "p90", "p99", "maximum"):
        assert actual[name] == round(value, 3), "duplicates preserve rounded ranks"
