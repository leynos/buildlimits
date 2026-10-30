"""The covered span and stated limits that every replay verdict carries."""

import dataclasses as dc
import datetime as dt

from outcome_test_helpers import OBSERVED_AT

from buildlimits import outcome_evidence
from buildlimits.outcome_sampling import CHILD_SAMPLE_SECONDS


@dc.dataclass(frozen=True)
class _Observed:
    """A minimal store-shaped value that records when it was observed.

    The span helpers accept any value with an ``observed_at`` member, so a
    stand-in keeps these tests independent of the record's full row layout.
    """

    observed_at: dt.datetime


def test_the_span_follows_the_observation_times_not_the_input_order() -> None:
    """The report states the oldest and newest record however they are ordered."""
    later = OBSERVED_AT + dt.timedelta(hours=3)
    coverage = outcome_evidence.coverage_of((_Observed(later), _Observed(OBSERVED_AT)))

    assert coverage.as_dict() == {
        "from": "2026-10-24T12:00:00Z",
        "until": "2026-10-24T15:00:00Z",
    }, "the span is the earliest and latest observation (R8)"


def test_a_non_utc_observation_is_reported_in_the_store_canonical_form() -> None:
    """A record stamped at an offset is reported as the same UTC instant."""
    offset = dt.timezone(dt.timedelta(hours=2))
    coverage = outcome_evidence.Coverage.over([
        dt.datetime(2026, 10, 24, 15, 0, tzinfo=offset),
    ])

    assert coverage.as_dict() == {
        "from": "2026-10-24T13:00:00Z",
        "until": "2026-10-24T13:00:00Z",
    }, "the span is spelled the way the store's own rows are spelled"


def test_an_empty_span_reports_no_bounds_rather_than_a_fabricated_one() -> None:
    """A store with no readable record has no span to state."""
    assert outcome_evidence.coverage_of(()).as_dict() == {"from": None, "until": None}


def test_the_stated_limits_name_the_bias_rather_than_a_count() -> None:
    """Each limit tells a reader how the sample may mislead them.

    H3 rests on window labels alone, so it carries only the quiet-window limit;
    H2 also reads child sightings, so it adds the sampling limit as well.
    """
    assert outcome_evidence.H3_LIMITS == (outcome_evidence.QUIET_WINDOW_CAVEAT,), (
        "H3 states the single limit its evidence inherits"
    )
    assert outcome_evidence.H2_LIMITS == (
        outcome_evidence.QUIET_WINDOW_CAVEAT,
        outcome_evidence.CHILD_SIGHTING_CAVEAT,
    ), "H2 states both limits in the order a reader should weigh them"
    assert f"{CHILD_SAMPLE_SECONDS:g} s" in outcome_evidence.CHILD_SIGHTING_CAVEAT, (
        "the stated interval matches the sampler's cadence (R9)"
    )
    assert (
        "solo and unanimous" in outcome_evidence.QUIET_WINDOW_CAVEAT
        and "busy, mixed" in outcome_evidence.QUIET_WINDOW_CAVEAT
    ), "the label limit names the selection and the traffic it misses (R2)"
