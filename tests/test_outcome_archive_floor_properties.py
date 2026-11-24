"""Properties of the retention floor's contiguous walk.

The floor may advance only across sequences a run deleted without leaving a
hole. A deletion beyond the first hole must not excuse that hole, because a
sequence the run did not delete may have been lost rather than removed.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from buildlimits.outcome_archive_state import contiguous_floor

_START = st.integers(min_value=0, max_value=10_000)
_OFFSET = st.integers(min_value=-5, max_value=80)
_SETTINGS = settings(deadline=None)


@_SETTINGS
@given(start=_START, offsets=st.lists(_OFFSET, max_size=50))
def test_the_floor_stops_at_the_first_sequence_not_deleted(
    start: int, offsets: list[int]
) -> None:
    """The floor is the first sequence at or above the start not removed."""
    deleted = {start + offset for offset in offsets}

    floor = contiguous_floor(start, deleted)

    assert floor >= start, "the floor never moves below its start"
    assert floor not in deleted, "the floor names a sequence the run did not delete"
    assert all(sequence in deleted for sequence in range(start, floor)), (
        "every sequence the floor passes was deleted by the run"
    )


@_SETTINGS
@given(
    start=_START,
    offsets=st.lists(_OFFSET, max_size=50),
    extra=st.integers(min_value=0, max_value=200),
)
def test_a_deletion_beyond_the_first_hole_does_not_advance_the_floor(
    start: int, offsets: list[int], extra: int
) -> None:
    """A deletion past the first hole cannot excuse a sequence below it."""
    deleted = {start + offset for offset in offsets}
    floor = contiguous_floor(start, deleted)
    beyond = floor + extra + 1

    assert contiguous_floor(start, deleted | {beyond}) == floor, (
        "a deletion beyond the first hole leaves the floor where it stopped"
    )
