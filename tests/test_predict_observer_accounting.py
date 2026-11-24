"""Observer accounting: appends apart from retain/release, and omissions by cause.

The observer folds the recorder's own journal events into the denominators the
report prints. Two rules keep those denominators honest: a compaction's retained
rows and a seal's released rows are not appends, and an outcome published
without a store row is counted by its closed reason. Both are measured here, so
a report can state one reconciliation rather than an unexplained difference.
"""

from outcome_test_helpers import OBSERVED_AT

from buildlimits.metrics_records import Record
from buildlimits.predict.journal_provenance import count_journal


def _store_result(
    operation: str, records: int, discarded: int, reason: str = "none"
) -> Record:
    """Return one outcome_store record for a writer result.

    Returns
    -------
    Record
        The aggregated writer result as the journal carries it.
    """
    return Record(
        "supervisor",
        "outcome_store",
        {
            "store_operation": operation,
            "store_reason": reason,
            "store_records": str(records),
            "store_discarded": str(discarded),
        },
        OBSERVED_AT,
    )


def _outcome(omission: str) -> Record:
    """Return one published cache outcome carrying an omission reason.

    Returns
    -------
    Record
        The per-launch outcome as the recorder emits it.
    """
    return Record(
        "supervisor",
        "cache_outcome",
        {
            "cache_result": "hit",
            "outcome_reason": "none",
            "outcome_attribution": "solo",
            "crate_origin": "registry",
            "record_omitted": omission,
        },
        OBSERVED_AT,
    )


def test_store_totals_count_appends_once_not_compaction_or_sealing() -> None:
    """A compact's retained rows and a seal's released rows are not appends.

    Counting every operation's ``store_records`` would report the same row
    several times and call the total an append count. Only an ``append`` result
    contributes to the append total; the operations stay visible in their own
    map so the retained and released figures are not lost.
    """
    observer = count_journal(
        (
            _store_result("append", records=4, discarded=0),
            _store_result("append", records=3, discarded=0),
            _store_result("compact", records=7, discarded=0),
            _store_result("seal", records=7, discarded=0),
        ),
        since="1h",
        cap=1000,
        truncated=False,
    ).observer

    assert observer is not None, "the counts are present"
    assert observer.store_records == 7, "two appends wrote seven rows in total"
    assert observer.by_store_operation == {
        "append": 2,
        "compact": 1,
        "seal": 1,
    }, "every operation is named"
    assert observer.store_discarded == 0, "nothing was dropped"


def test_a_mixed_operation_history_still_counts_only_appends() -> None:
    """The append total is right even when most results are not appends."""
    observer = count_journal(
        (
            _store_result("append", records=1, discarded=0),
            _store_result("compact", records=9, discarded=0),
            _store_result("seal", records=9, discarded=0),
            _store_result("load", records=9, discarded=0),
        ),
        since="1h",
        cap=1000,
        truncated=False,
    ).observer

    assert observer is not None, "the counts are present"
    assert observer.store_records == 1, "one row was appended"
    assert sum(observer.by_store_operation.values()) == 4, (
        "every result is counted in its own operation"
    )


def test_published_outcomes_reconcile_as_rows_plus_omissions() -> None:
    """Each published outcome is an appended row or an omitted one by cause.

    A launch with no key or no label moment publishes an outcome but produces
    no store row, so the writer's own discard counts can never see it. The
    observer counts those omissions by their closed reason and the two totals
    reconcile: published outcomes = appended rows + omitted ones.
    """
    appended = (
        _store_result("append", records=3, discarded=0),
        _store_result("append", records=2, discarded=0),
    )
    outcomes = tuple(
        _outcome(omission)
        for omission in ("none", "none", "none", "none", "none", "key_unavailable")
    )

    observer = count_journal(
        (*outcomes, *appended), since="1h", cap=1000, truncated=False
    ).observer

    assert observer is not None, "the counts are present"
    assert observer.outcomes == 6, "six outcomes were published"
    assert observer.record_omitted == 1, "one produced no store row"
    assert observer.by_omission_reason == {"key_unavailable": 1}, (
        "the omission is counted by its closed reason"
    )
    assert observer.store_records == 5, "five rows were appended"
    assert observer.outcomes == observer.store_records + observer.record_omitted, (
        "published outcomes reconcile as appended rows plus omitted ones"
    )


def test_an_outcome_without_an_omission_field_counts_no_omission() -> None:
    """An older recorder's event is not read as a measured omission."""
    observer = count_journal(
        (
            Record(
                "supervisor",
                "cache_outcome",
                {
                    "cache_result": "hit",
                    "outcome_reason": "none",
                    "outcome_attribution": "solo",
                    "crate_origin": "registry",
                },
                OBSERVED_AT,
            ),
        ),
        since="1h",
        cap=1000,
        truncated=False,
    ).observer

    assert observer is not None, "the counts are present"
    assert observer.outcomes == 1, "the outcome is still counted"
    assert observer.record_omitted == 0, "absence is not a measured omission"
    assert observer.by_omission_reason == {}, "and it names no reason"
