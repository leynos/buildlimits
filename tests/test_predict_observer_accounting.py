"""Observer accounting: appends apart from retain/release, and omissions by cause.

The observer folds the recorder's own journal events into the denominators the
report prints. Two rules keep those denominators honest: a compaction's retained
rows and a seal's released rows are not appends, and an outcome published
without a store row is counted by its closed reason. Both are measured here, but
neither makes the four categories a proof of identity: the counts cannot be
correlated term by term, so the report names the categories a published outcome
can fall into and says it does not prove it.
"""

import typing as typ

from journal_fixtures import journal_lines, journal_runner
from outcome_test_helpers import OBSERVED_AT

from buildlimits import metrics_schema
from buildlimits.metrics import Metrics
from buildlimits.metrics_query import query
from buildlimits.metrics_records import Record
from buildlimits.predict.journal_provenance import count_journal, observer_accounting

if typ.TYPE_CHECKING:
    from conftest import RecordingSink

    from buildlimits.outcome_vocabulary import RecordOmission


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


def test_published_outcomes_split_between_appends_and_omissions() -> None:
    """A published outcome is an appended row, an omission, a discard or queued.

    A launch with no key or no label moment publishes an outcome but produces
    no store row, so the writer's own discard counts can never see it. The
    observer counts those omissions by their closed reason. In a window that
    holds no window summary, no discard and no queued write, the two totals do
    account for every outcome; the report does not generalize that arithmetic,
    because those other categories are counted differently or not at all.
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
        "in this window every outcome is an appended row or an omission, but the "
        "report does not prove that identity in general"
    )


def test_an_outcome_without_an_omission_field_is_counted_as_unrecorded() -> None:
    """An older recorder's event is not read as a measured ``none``.

    An event that carries no omission field says nothing about whether its
    outcome was stored. Counting it as a measured zero would make the window
    indistinguishable from one whose outcomes were all stored, so it is
    counted apart as unrecorded.
    """
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
    assert observer.record_omitted_unrecorded == 1, (
        "the missing evidence is counted as unavailable, not as none"
    )


def test_a_measured_stored_window_records_no_unrecorded_omission() -> None:
    """A window the recorder tagged with ``none`` is not called unrecorded."""
    observer = count_journal(
        (
            _outcome("none"),
            _outcome("none"),
            _store_result("append", records=2, discarded=0),
        ),
        since="1h",
        cap=1000,
        truncated=False,
    ).observer

    assert observer is not None, "the counts are present"
    assert observer.record_omitted == 0, "nothing was omitted"
    assert observer.record_omitted_unrecorded == 0, (
        "a stated none is measured evidence, so it is not unrecorded"
    )


_OMISSION_REASONS: tuple[RecordOmission, ...] = (
    "none",
    "key_unavailable",
    "label_time_unavailable",
)
"""Every closed omission reason, which the coverage check pins to the schema."""


def test_every_omission_token_reaches_the_observer_through_the_real_schema(
    metric_sink: RecordingSink,
) -> None:
    """Each closed omission reason survives emit, journal decode and counting.

    The path from the recorder to the report crosses three boundaries that can
    each lose the evidence: the metrics schema refuses a field whose kind is
    wrong, the native-journal decoder keeps only schema-owned fields, and the
    observer counts a token rather than a counter. This drives one
    ``cache_outcome`` per closed ``RecordOmission`` value through the real
    :class:`~buildlimits.metrics.Metrics` sink and the real journal query, then
    folds the records the query returned. An explicit ``none`` states that no
    pre-store omission applied, so it must not appear as an omission; the real
    reasons must, and their total must equal the events that reported one. It
    does not claim the row was stored: a write can still fail or remain
    queued.
    """
    assert set(_OMISSION_REASONS) == metrics_schema.RECORD_OMISSIONS, (
        "the cases cover the whole closed vocabulary, so a new reason fails here"
    )
    metrics = Metrics(metric_sink)
    for omission in _OMISSION_REASONS:
        emitted = metrics.emit(
            "cache_outcome",
            cache_result="hit",
            outcome_reason="none",
            outcome_attribution="solo",
            crate_origin="registry",
            record_omitted=omission,
        )
        assert emitted, f"the {omission} event is inside the schema"
    assert metrics.dropped_fields == 0, "the schema refused no field of the events"
    assert len(metric_sink.records) == len(_OMISSION_REASONS), (
        "every event reached the sink"
    )

    lines = journal_lines((record, 0.0) for record in metric_sink.records)
    observer = observer_accounting(query(runner=journal_runner(lines)).records)

    # The expectation is read back from what the sink actually kept, so the
    # assertion proves the journal carried each reason rather than that this
    # test can guess the answer from its own input table.
    reported = [
        record["BUILD_LIMITS_RECORD_OMITTED"]
        for record in metric_sink.records
        if record["BUILD_LIMITS_RECORD_OMITTED"] != "none"
    ]
    assert observer.by_omission_reason == {
        "key_unavailable": 1,
        "label_time_unavailable": 1,
    }, "both real reasons are counted by their closed token"
    assert set(observer.by_omission_reason) == set(reported), (
        "an explicit none means no pre-store omission, so it names no reason"
    )
    assert observer.record_omitted == len(reported), (
        "the omitted total is the events that reported one"
    )
    assert observer.record_omitted_unrecorded == 0, (
        "every event stated its omission, so none is unrecorded"
    )
    assert observer.outcomes == len(_OMISSION_REASONS), "every event is an outcome"
