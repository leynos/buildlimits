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
from buildlimits.outcome_retention import RetentionPolicy

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


_COMPACTION_NOW = dt.datetime(2027, 1, 1, tzinfo=dt.UTC)


def _generated_rows() -> st.SearchStrategy[list[tuple[int, str]]]:
    """Generate observation ages and version tags for one store."""
    return st.lists(
        st.tuples(
            st.integers(min_value=-120, max_value=120),
            st.text(alphabet="abcdef", min_size=0, max_size=8),
        ),
        min_size=0,
        max_size=20,
    )


@settings(max_examples=60, deadline=None)
@given(
    rows=_generated_rows(),
    age_days=st.integers(min_value=0, max_value=90),
    max_bytes=st.integers(min_value=0, max_value=50_000),
    max_records=st.one_of(st.none(), st.integers(min_value=0, max_value=20)),
)
def test_generated_compaction_respects_every_bound(
    rows: list[tuple[int, str]],
    age_days: int,
    max_bytes: int,
    max_records: int | None,
) -> None:
    """Generated records, ages and bounds always leave a lawful suffix.

    The writer-side compaction is the one retention path that rewrites the
    active file in place. For any generated store and policy it must drop only
    a prefix of the age-retained rows, count every dropped row, honour the
    byte and record bounds when it keeps anything, and be idempotent when the
    settled document is compacted again.
    """
    now = _COMPACTION_NOW
    records = tuple(
        window_summary(
            server_version=f"v{tag}",
            observed_at=now - dt.timedelta(days=age),
        )
        for age, tag in rows
    )
    policy = RetentionPolicy(
        max_age=dt.timedelta(days=age_days),
        max_bytes=max_bytes,
        max_records=max_records,
        headroom_bytes=1 << 20,
        segment_bytes=1 << 20,
    )
    decoded = outcome_records.DecodedStore(
        outcome_records.StoreHeader("generated-salt", now), records
    )

    compacted = outcome_records.compact(decoded, now=now, policy=policy)

    cutoff = policy.cutoff(now)
    aged = (
        list(records)
        if cutoff is None
        else [record for record in records if record.observed_at >= cutoff]
    )
    assert compacted.records == tuple(aged[len(aged) - len(compacted.records) :]), (
        "compaction drops only a prefix of the age-retained rows"
    )
    assert compacted.discarded == len(records) - len(compacted.records), (
        "every dropped row is counted once"
    )

    retained_size = len(outcome_records.encode_line(compacted.header).encode("utf-8"))
    retained_size += 1
    retained_size += sum(
        len(outcome_records.encode_line(record).encode("utf-8")) + 1
        for record in compacted.records
    )
    if policy.has_byte_limit and compacted.records:
        assert retained_size <= policy.max_bytes, (
            "the byte bound holds when rows remain"
        )
    if max_records is not None:
        assert len(compacted.records) <= max_records, "the record cap holds"

    again = outcome_records.compact(
        outcome_records.DecodedStore(compacted.header, compacted.records),
        now=now,
        policy=policy,
    )
    assert again.discarded == 0, "a settled document drops nothing on re-compaction"
    assert again.records == compacted.records, "re-compaction keeps the same rows"
