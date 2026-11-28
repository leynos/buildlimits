"""The bounded counters the cache-outcome recorder publishes in status.

Only outcome rows carry correlation evidence, and a row the writer accepts is
not yet a row it stored. These tests pin both distinctions: a window summary is
never counted as an uncorrelated outcome, and the correlation counts name rows
offered to the writer, with a refused write reported separately as a discarded
store record.
"""

from outcome_recorder_test_helpers import OBSERVED_AT, stored_record

from buildlimits.outcome_record_format import StoreRecord, WindowSummary
from buildlimits.outcome_shell_stats import OutcomeCounters

_ATTEMPT_LIMIT = 4096


def _counted(*rows: StoreRecord) -> OutcomeCounters:
    """Return counters after offering ``rows`` to the writer.

    Returns
    -------
    OutcomeCounters
        The counters the shell would publish.
    """
    counters = OutcomeCounters()
    for row in rows:
        counters.note_record(row)
    return counters


def test_a_complete_pair_is_correlated_and_not_a_repeat() -> None:
    """The first row of one attempt counts as neither uncorrelated nor a repeat."""
    counters = _counted(stored_record(attempt="g1", generation="gen"))

    assert counters.offered_uncorrelated_rows == 0, "the row names its invocation"
    assert counters.offered_repeated_attempts == 0, "it is the first sighting"


def test_a_row_without_a_pair_is_uncorrelated() -> None:
    """A legacy row names no invocation, so it is counted as uncorrelated."""
    counters = _counted(stored_record(attempt=None, generation=None))

    assert counters.offered_uncorrelated_rows == 1, "no attempt pair is recorded"
    assert counters.offered_repeated_attempts == 0, "and it cannot repeat one"


def test_half_a_pair_is_uncorrelated() -> None:
    """A row stating one half cannot be joined, so it is uncorrelated too."""
    counters = _counted(stored_record(attempt="g1", generation=None))

    assert counters.offered_uncorrelated_rows == 1, "half a pair is no identity"


def test_a_repeated_pair_is_counted_once_per_repeat() -> None:
    """The second and third rows of one attempt are repeats, the first is not."""
    counters = _counted(
        *(stored_record(attempt="g1", generation="gen") for _copy in range(3))
    )

    assert counters.offered_repeated_attempts == 2, "two rows repeated the identity"
    assert counters.offered_uncorrelated_rows == 0, "every row named the attempt"


def test_the_identity_index_is_bounded() -> None:
    """The index forgets the oldest identity rather than growing without limit."""
    counters = _counted(
        *(
            stored_record(attempt=f"g{index}", generation="gen")
            for index in range(_ATTEMPT_LIMIT + 1)
        ),
        stored_record(attempt="g0", generation="gen"),
    )

    assert counters.offered_repeated_attempts == 0, (
        "the evicted identity is not remembered, so its return is not a repeat"
    )


def test_a_window_summary_is_not_an_outcome_row() -> None:
    """A summary carries no correlation evidence and is never counted."""
    summary = WindowSummary(
        observed_at=OBSERVED_AT,
        server_version="0.14.0",
        window_groups=1,
        requests_not_compile=0,
        requests_not_cacheable=0,
        predicted_not_compile=0,
        predicted_declined=0,
        predicted_cacheable=1,
    )

    counters = _counted(summary)

    assert counters.offered_uncorrelated_rows == 0, "a summary is not a legacy row"
    assert counters.offered_repeated_attempts == 0, "nor a repeated attempt"


def test_the_status_payload_carries_both_counts() -> None:
    """`describe` publishes the counts, so the status section reads them."""
    counters = _counted(
        stored_record(attempt=None, generation=None),
        stored_record(attempt="g1", generation="gen"),
        stored_record(attempt="g1", generation="gen"),
    )

    described = counters.describe()

    assert described["offered_uncorrelated_rows"] == 1, "the legacy row is counted"
    assert described["offered_repeated_attempts"] == 1, "the repeat is counted"
