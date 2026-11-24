"""Label-loss accounting stays exact and never invents evidence."""

import dataclasses as dc
import datetime as dt
import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st
from outcome_store_helpers import window_summary
from outcome_test_helpers import OBSERVED_AT, attributed_record

from buildlimits.predict import accounting as accounting_module
from buildlimits.predict.accounting import (
    UNRECORDED,
    HistoryLoss,
    checked_counts,
    store_accounting,
)

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import OutcomeRecord
    from buildlimits.outcome_vocabulary import CacheResult, OutcomeReason

ADMITTED_AT = OBSERVED_AT - dt.timedelta(seconds=2)
RESULTS = ("hit", "miss", "unattributed", "declined", "failed", "error")
REASONS = (
    "none",
    "mixed_window",
    "count_mismatch",
    "ineligible_window",
    "timeout",
    "window_overflow",
)
ATTRIBUTIONS = ("solo", "unanimous", "none")


def _record(
    *,
    outcome: str,
    reason: str | None,
    attribution: str | None,
    admitted_at: dt.datetime | None,
) -> OutcomeRecord:
    """Return one registry record with the supplied closed-vocabulary evidence."""
    return dc.replace(
        attributed_record(outcome=typ.cast("CacheResult", outcome)),
        outcome_reason=typ.cast("OutcomeReason | None", reason),
        outcome_attribution=typ.cast("typ.Any", attribution),
        admitted_at=admitted_at,
    )


def test_every_breakdown_sums_to_the_outcome_rows() -> None:
    """Each map is measured against the same per-compilation denominator."""
    records = (
        _record(
            outcome="hit",
            reason="none",
            attribution="solo",
            admitted_at=ADMITTED_AT,
        ),
        dc.replace(
            _record(
                outcome="miss",
                reason="none",
                attribution="unanimous",
                admitted_at=ADMITTED_AT,
            ),
            origin="git",
        ),
        dc.replace(
            _record(
                outcome="unattributed",
                reason="mixed_window",
                attribution="none",
                admitted_at=None,
            ),
            origin="path",
        ),
        _record(
            outcome="declined",
            reason="none",
            attribution="solo",
            admitted_at=ADMITTED_AT,
        ),
        window_summary(observed_at=OBSERVED_AT),
    )

    accounting = store_accounting(typ.cast("typ.Any", records))

    assert accounting.records_total == 5, "summaries count towards rows read"
    assert accounting.outcome_records == 4, "only keyed rows are outcome rows"
    assert accounting.summary_records == 1, "the summary is named separately"
    assert sum(accounting.by_result.values()) == 4, "results reconcile"
    assert sum(accounting.by_reason.values()) == 4, "reasons reconcile"
    assert sum(accounting.by_attribution.values()) == 4, "attribution reconciles"
    assert sum(accounting.by_origin.values()) == 4, "origins reconcile"
    assert sum(accounting.by_parser_class.values()) == 4, "parser classes reconcile"
    assert sum(accounting.by_child_sighting.values()) == 4, "sightings reconcile"
    assert sum(accounting.by_server_version.values()) == 4, "versions reconcile"
    assert accounting.labels == 2, "only hit and miss are labels"
    assert accounting.cacheable_records == 4, "every fixture row is cacheable"
    assert accounting.cacheable_labels == 2, "two cacheable rows are labelled"
    assert accounting.by_result["declined"] == 1, "declined is never a miss"
    assert accounting.by_reason["mixed_window"] == 1, "the refusal is named"
    assert accounting.admission_recorded == 3, "three rows carry admission"
    assert accounting.admission_missing == 1, "the fourth does not"
    assert accounting.labelled_fraction == pytest.approx(0.5), "two of four rows"


def test_missing_evidence_is_named_unrecorded() -> None:
    """A pre-v2 row reports absence rather than a guessed reason or time."""
    accounting = store_accounting((attributed_record(outcome="hit"),))

    assert accounting.by_reason == {UNRECORDED: 1}, "no reason is invented"
    assert accounting.by_attribution == {UNRECORDED: 1}, "no attribution is invented"
    assert accounting.admission_missing == 1, "no admission time is invented"
    assert accounting.admission_recorded == 0, "nothing was recorded"
    assert accounting.labels == 1, "the result itself is still a label"


@given(
    rows=st.lists(
        st.tuples(
            st.sampled_from(RESULTS),
            st.one_of(st.none(), st.sampled_from(REASONS)),
            st.one_of(st.none(), st.sampled_from(ATTRIBUTIONS)),
            st.booleans(),
        ),
        max_size=30,
    ),
)
def test_accounting_invariants_hold_for_any_bounded_stream(
    rows: list[tuple[str, str | None, str | None, bool]],
) -> None:
    """Totals reconcile for every generated mix of closed-vocabulary rows."""
    records = tuple(
        _record(
            outcome=outcome,
            reason=reason,
            attribution=attribution,
            admitted_at=ADMITTED_AT if admitted else None,
        )
        for outcome, reason, attribution, admitted in rows
    )

    accounting = store_accounting(typ.cast("typ.Any", records))

    assert accounting.records_total == len(rows), "every row counted once"
    assert accounting.outcome_records == len(rows), "every row is an outcome row"
    for counts in (
        accounting.by_result,
        accounting.by_reason,
        accounting.by_attribution,
        accounting.by_origin,
        accounting.by_parser_class,
        accounting.by_child_sighting,
        accounting.by_server_version,
    ):
        assert sum(counts.values()) == len(rows), "each breakdown reconciles"
    assert accounting.admission_recorded + accounting.admission_missing == len(rows), (
        "admission counts reconcile"
    )
    assert accounting.labels == sum(
        1 for outcome, *_rest in rows if outcome in {"hit", "miss"}
    ), "only hit and miss are labels"
    assert accounting.cacheable_records == len(rows), "every row is cacheable"
    assert accounting.cacheable_labels == accounting.labels, (
        "every label is on a cacheable row"
    )
    assert 0.0 <= accounting.labelled_fraction <= 1.0, "the share is a fraction"


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ({"hit": -1}, ValueError),
        ({"hit": 1, "miss": True}, ValueError),
        ({1: 2}, TypeError),
        ([("hit", 1)], TypeError),
        ({f"k{index}": 1 for index in range(65)}, ValueError),
    ],
    ids=["negative", "boolean", "non-text-key", "not-a-map", "over-bound"],
)
def test_checked_counts_refuses_invalid_maps(
    value: object, error: type[Exception]
) -> None:
    """A snapshot count map is validated before a report can print it."""
    with pytest.raises(error):
        checked_counts(value, label="store")


def test_history_loss_defaults_to_the_active_file() -> None:
    """A read that considered no closed segment says so explicitly."""
    loss = HistoryLoss.active_file_only()

    assert loss.active_only is True, "closed history was not considered"
    assert loss.inventory_unavailable is False, "the absence was a choice"
    assert loss.gaps == 0, "no gap count is invented"
    assert loss.retention_floor is None, "no floor is invented"


def test_a_breakdown_over_the_entry_bound_keeps_its_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overflow values merge into one bucket so the breakdown still sums."""
    monkeypatch.setattr(accounting_module, "_MAX_MAP_ENTRIES", 2)
    base = attributed_record(outcome="hit")
    records = tuple(
        dc.replace(
            base,
            key=dc.replace(
                base.key,
                toolchain_identity=dc.replace(
                    base.key.toolchain_identity, server_version=version
                ),
            ),
        )
        for version in ("0.12.0", "0.13.0", "0.14.0")
    )

    accounting = store_accounting(typ.cast("typ.Any", records))

    assert len(accounting.by_server_version) == 2, "the map stays within its bound"
    assert sum(accounting.by_server_version.values()) == 3, "no count is dropped"
    assert accounting.by_server_version["other"] == 2, "the overflow is explicit"
