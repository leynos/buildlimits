"""Property contracts for the loss-accounting and timestamp values.

Three invariants are generated rather than enumerated. The timestamp round trip
must hold for every UTC moment the store can write. The diagnostics decoder must
accept exactly the documents that reconcile and refuse the rest, whatever counts
a generator mixes. And the recorder must treat every completion sharing one
reading moment alike, so an out-of-order finish can never change what a window
records.

Each test is derandomized and bounded so a failure is reproducible and the suite
stays fast.
"""

import collections.abc as cabc
import datetime as dt

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from outcome_reading_test_helpers import delayed_reading_rows

from buildlimits.outcome_record_time import (
    INVALID_TIMESTAMP,
    optional_timestamp,
    parse_timestamp,
    timestamp_text,
)
from buildlimits.predict.accounting import Diagnostics, HistoryLoss, StoreAccounting
from buildlimits.predict.diagnostics_codec import (
    diagnostics_from_dict,
    diagnostics_to_dict,
)

# The generated range stays well inside what a UTC datetime can hold, so a
# conversion cannot fail on an edge the test never meant to probe.
_EPOCH_LOW = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_EPOCH_HIGH = dt.datetime(2100, 1, 1, tzinfo=dt.UTC)
_MAX_COUNT = 50


def _utc_moment() -> st.SearchStrategy[dt.datetime]:
    """Generate an aware UTC moment within a representable range.

    Returns
    -------
    st.SearchStrategy[datetime]
        Times from 1970 to 2100, always carrying the UTC timezone.
    """
    return st.datetimes(
        min_value=_EPOCH_LOW, max_value=_EPOCH_HIGH, timezones=st.just(dt.UTC)
    )


@given(_utc_moment())
@settings(max_examples=50, derandomize=True, deadline=None)
def test_a_utc_moment_round_trips_through_its_canonical_text(
    moment: dt.datetime,
) -> None:
    """Encoding then parsing returns the same instant, to the microsecond."""
    text = timestamp_text(moment)

    assert text.endswith("Z"), "the canonical form names UTC with a trailing Z"
    assert "+00:00" not in text, "the offset is never written in the long form"
    assert parse_timestamp(text) == moment, "the instant survives the round trip"


@given(
    st.datetimes(min_value=_EPOCH_LOW, max_value=_EPOCH_HIGH),
    st.integers(min_value=1, max_value=23),
)
@settings(max_examples=50, derandomize=True, deadline=None)
def test_a_non_utc_offset_is_refused_rather_than_relabelled(
    naive: dt.datetime, hours: int
) -> None:
    """Only UTC text is a moment this store reads back.

    A local offset names the same wall-clock reading as an instant the store
    never recorded, and converting it would silently invent a moment from a
    clock the recorder does not use. The parser therefore refuses it, and the
    refusal is distinguishable from absence: it returns ``None`` for a value it
    could not read, never a shifted instant.
    """
    offset = dt.timezone(dt.timedelta(hours=hours))
    moment = naive.replace(tzinfo=offset)

    parsed = parse_timestamp(moment.isoformat())

    assert parsed is None, "a non-UTC offset is not a stored moment"
    assert optional_timestamp(moment.isoformat()) is INVALID_TIMESTAMP, (
        "and a present but unusable value is corruption, not absence"
    )


@given(_utc_moment())
@settings(max_examples=50, derandomize=True, deadline=None)
def test_utc_text_is_normalized_to_the_utc_timezone(moment: dt.datetime) -> None:
    """An accepted moment is always the UTC instant the store names.

    The ``+00:00`` spelling and the canonical ``Z`` spelling denote the same
    moment, and both parse to a value whose offset is exactly zero, so no
    caller has to normalize a timestamp it read back.
    """
    explicit = moment.replace(tzinfo=dt.UTC).isoformat()

    parsed = parse_timestamp(explicit)

    assert parsed is not None, "a zero offset is the accepted form"
    assert parsed.tzinfo is dt.UTC or parsed.utcoffset() == dt.timedelta(0), (
        "the parsed value is UTC rather than the text's original zone object"
    )
    assert parsed == moment, "the instant is preserved exactly"


@given(
    st.one_of(
        st.none(),
        st.text(max_size=40),
        st.floats(allow_nan=True, allow_infinity=True),
        st.integers(),
    )
)
@settings(max_examples=50, derandomize=True, deadline=None)
def test_an_optional_timestamp_separates_absence_from_corruption(
    value: object,
) -> None:
    """None is absence, a valid timestamp is a moment, and anything else is invalid."""
    parsed = optional_timestamp(value)

    if value is None:
        assert parsed is None, "an explicit null is absence, not corruption"
    elif isinstance(parsed, dt.datetime):
        assert parsed.utcoffset() == dt.timedelta(0), "a parsed moment is UTC"
        assert timestamp_text(parsed) and parse_timestamp(timestamp_text(parsed)) == (
            parsed
        ), "a parsed moment survives the canonical form"
    else:
        assert parsed is INVALID_TIMESTAMP, (
            "a present value that is not a UTC timestamp is corruption"
        )


def _counts(**overrides: object) -> dict[str, object]:
    """Return a reconciling store-counts block with the overrides applied.

    Returns
    -------
    dict[str, object]
        A store block whose every breakdown sums to its outcome rows.
    """
    block: dict[str, object] = {
        "records_total": 3,
        "outcome_records": 3,
        "summary_records": 0,
        "admission_recorded": 2,
        "admission_missing": 1,
        "labels": 2,
        "cacheable_records": 3,
        "cacheable_labels": 2,
        "by_result": {"hit": 1, "miss": 1, "unattributed": 1},
        "by_reason": {"none": 2, "mixed_window": 1},
        "by_attribution": {"solo": 2, "none": 1},
        "by_origin": {"registry": 3},
        "by_parser_class": {"cacheable": 3},
        "by_child_sighting": {"unavailable": 3},
        "by_server_version": {"0.14.0": 3},
        "by_generation": {"unrecorded": 3},
        "labels_by_generation": {"unrecorded": 2},
    }
    block.update(overrides)
    return block


def _document(**store_overrides: object) -> dict[str, object]:
    """Return a diagnostics document holding one possibly-broken store block.

    Returns
    -------
    dict[str, object]
        The encoded diagnostics, ready for the decoder.
    """
    return {
        "store": _counts(**store_overrides),
        "history": {
            "active_only": False,
            "archived": 0,
            "sealed": 0,
            "gaps": 0,
            "gaps_truncated": False,
            "retention_floor": None,
            "quarantined": 0,
            "inventory_unavailable": False,
        },
    }


@given(
    outcomes=st.integers(min_value=0, max_value=_MAX_COUNT),
    summaries=st.integers(min_value=0, max_value=_MAX_COUNT),
)
@settings(max_examples=50, derandomize=True, deadline=None)
def test_generated_reconciling_counts_survive_the_round_trip(
    outcomes: int, summaries: int
) -> None:
    """The decoder accepts and restores every block whose counts reconcile.

    Each breakdown is drawn from the same generated totals, so every document
    this test builds really reconciles. The refusal of a block that does not is
    a separate contract, below, because a single generator cannot assert both
    outcomes for one document.
    """
    store = _counts(**{
        "records_total": outcomes + summaries,
        "outcome_records": outcomes,
        "summary_records": summaries,
        "admission_recorded": outcomes,
        "admission_missing": 0,
        "labels": 0,
        "cacheable_records": outcomes,
        "cacheable_labels": 0,
        "by_result": {"unattributed": outcomes} if outcomes else {},
        "by_reason": {"none": outcomes} if outcomes else {},
        "by_attribution": {"none": outcomes} if outcomes else {},
        "by_origin": {"registry": outcomes} if outcomes else {},
        "by_parser_class": {"cacheable": outcomes} if outcomes else {},
        "by_child_sighting": {"unavailable": outcomes} if outcomes else {},
        "by_server_version": {"0.14.0": outcomes} if outcomes else {},
        "by_generation": {"unrecorded": outcomes} if outcomes else {},
        "labels_by_generation": {"unrecorded": 0} if outcomes else {},
    })
    document = {
        "store": store,
        "history": {
            "active_only": True,
            "archived": 0,
            "sealed": 0,
            "gaps": 0,
            "gaps_truncated": False,
            "retention_floor": None,
            "quarantined": 0,
            "inventory_unavailable": False,
        },
    }

    decoded = diagnostics_from_dict(document, label="generated")

    assert decoded.store.outcome_records == outcomes, "the row total survives"
    assert decoded.store.records_total == outcomes + summaries, "and the summaries"
    assert diagnostics_to_dict(decoded) == document, (
        "the encoder restores the same block the decoder accepted"
    )


@pytest.mark.parametrize(
    ("broken", "message"),
    [
        ({"records_total": 4}, "totals do not reconcile"),
        ({"outcome_records": 4}, "totals do not reconcile"),
        ({"admission_recorded": 3}, "admission counts do not sum"),
        ({"labels": 1}, "disagrees with its result breakdown"),
        ({"by_reason": {"none": 2}}, "breakdowns do not sum"),
        ({"by_origin": {"registry": 2}}, "breakdowns do not sum"),
    ],
    ids=[
        "record-total",
        "outcome-total",
        "admission-total",
        "label-total",
        "result-breakdown",
        "origin-breakdown",
    ],
)
def test_a_generated_inconsistency_is_always_refused(
    broken: cabc.Mapping[str, object], message: str
) -> None:
    """A block that cannot describe one history never becomes evidence."""
    document = _document(**dict(broken))

    with pytest.raises(ValueError, match=message):
        diagnostics_from_dict(document, label="generated")


def _diagnostics() -> Diagnostics:
    """Return a reconciling diagnostics value for the reconstruction check.

    Returns
    -------
    Diagnostics
        A value whose encoded form the decoder must restore unchanged.
    """
    decoded = diagnostics_from_dict(_document(), label="generated")
    assert isinstance(decoded, Diagnostics), "a reconciling block decodes"
    return decoded


def test_the_decoder_restores_the_reconciling_value_it_encoded() -> None:
    """A value and its encoded form agree field for field."""
    value = _diagnostics()

    restored = diagnostics_from_dict(diagnostics_to_dict(value), label="generated")

    assert restored == value, "every count survives the trip"
    assert isinstance(restored.store, StoreAccounting), "the store block is typed"
    assert isinstance(restored.history, HistoryLoss), "the history block is typed"


@given(
    st.lists(
        st.datetimes(
            min_value=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            max_value=dt.datetime(2026, 1, 2, tzinfo=dt.UTC),
        ),
        min_size=1,
        max_size=8,
    )
)
@settings(max_examples=30, derandomize=True, deadline=None)
def test_completions_sharing_a_reading_moment_share_one_recorded_time(
    completions: list[dt.datetime],
) -> None:
    """Every launch buffered at one reading records that reading's moment.

    The completions are deliberately out of order, so a recorder that dated each
    row at its own launch would write a history whose clock runs backwards in
    append order. The contract is order-independent: whatever sequence the
    completions arrive in, each row and every window summary carries the one
    reading moment, and the store order stays monotonic.
    """
    reading_at = dt.datetime(2026, 1, 2, tzinfo=dt.UTC)

    records = delayed_reading_rows(
        completions=tuple(completions), reading_at=reading_at
    )

    recorded = [record.observed_at for record in records]

    assert recorded == [reading_at] * len(records), (
        "every row and the summary carry the one reading moment"
    )
    assert recorded == sorted(recorded), (
        "so store order is monotonic however the completions were ordered"
    )
