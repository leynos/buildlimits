"""The real record cap compacts exactly the oldest row.

The generated state machine samples operation sequences, so a cap crossed once
by a deliberately generated store is the case it cannot promise to reach. This
example pins the bound itself: a store one row past the real cap loses exactly
its oldest row, and the report counts that row as discarded.
"""

import datetime as dt

from hypothesis import given, settings
from hypothesis import strategies as st
from outcome_store_helpers import window_summary
from outcome_test_helpers import record_generated_case

from buildlimits import outcome_records

_BASE_TIME = dt.datetime(2026, 10, 25, tzinfo=dt.UTC)


def test_generated_compaction_reaches_the_retention_bound() -> None:
    """Generated compact input evicts only the oldest row at the real bound."""
    reached: set[str] = set()

    @settings(max_examples=1, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=10_000))
    def verify(seed: int) -> None:
        """Generate a valid store just beyond its real retention bound."""
        now = _BASE_TIME
        header = outcome_records.StoreHeader("generated-salt", now)
        records = tuple(
            window_summary(server_version=f"{seed}-{index}", observed_at=now)
            for index in range(outcome_records.MAX_COMPACT_RECORDS + 1)
        )

        compacted = outcome_records.compact(
            outcome_records.DecodedStore(header, records), now=now
        )

        assert compacted.records == records[1:], (
            "the count bound retains the newest rows in insertion order"
        )
        assert compacted.discarded == 1, "only the oldest row is counted as evicted"
        reached.add("retention-bound")
        record_generated_case("retention-bound")

    verify()

    assert reached == {"retention-bound"}, (
        "generation crossed the actual 10,000-row cap"
    )
