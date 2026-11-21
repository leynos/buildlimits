"""The bounds a retention policy refuses and the age cutoff it derives.

A policy is validated once, when settings become the effective policy, so an
unusable bound must be refused there rather than at the writer's first append.
The cutoff is derived from a caller-supplied UTC time and must never raise for
an extreme age; shortening it to the representable minimum keeps the store's
oldest history retained rather than losing the whole read.
"""

import dataclasses as dc
import datetime as dt
import typing as typ

import pytest

from buildlimits.config import SupervisorConfig
from buildlimits.outcome_record_encode import header_floor_bytes
from buildlimits.outcome_retention import (
    LEGACY_MAX_RECORDS,
    MAX_AGE_DAYS,
    MAX_POLICY_VALUE,
    RetentionPolicy,
    policy_from_settings,
    validate_policy,
)


def _settings(**overrides: object) -> SupervisorConfig:
    """Return the real supervisor settings with the given retention values.

    Parameters
    ----------
    overrides : object
        Retention field values the test means to configure.

    Returns
    -------
    SupervisorConfig
        A complete settings value, defaulted everywhere else.
    """
    return dc.replace(SupervisorConfig(), **overrides)


def test_an_explicit_record_cap_survives_segmented_mode() -> None:
    """An operator's cap applies whether or not the store rotates."""
    policy = policy_from_settings(
        _settings(cache_outcomes_max_records=500), header_floor=header_floor_bytes()
    )

    assert policy.max_records == 500, "the explicit cap is used as given"


def test_legacy_mode_keeps_the_historical_record_cap() -> None:
    """A legacy store with no configured cap keeps the 10,000-row bound."""
    policy = policy_from_settings(
        _settings(cache_outcomes_max_bytes=1_000_000), header_floor=header_floor_bytes()
    )

    assert policy.max_records == LEGACY_MAX_RECORDS, "the legacy cap applies"
    assert not policy.is_segmented, "a budget under one segment stays legacy"


def test_segmented_mode_without_a_cap_has_no_row_bound() -> None:
    """Rotation bounds the store, so no record cap is imposed by default."""
    policy = policy_from_settings(
        _settings(cache_outcomes_max_bytes=15_000_000_000),
        header_floor=header_floor_bytes(),
    )

    assert policy.max_records is None, "an implied cap would shorten history"
    assert policy.is_segmented, "a budget over one segment rotates the store"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"segment_bytes": 0}, "segment_bytes must be positive"),
        ({"headroom_bytes": 1, "segment_bytes": 2}, "at least one segment"),
        ({"max_age": dt.timedelta(days=-1)}, "must not be negative"),
        ({"max_age": dt.timedelta(days=MAX_AGE_DAYS + 1)}, "is too large"),
        ({"max_bytes": -1}, "max_bytes is outside the supported range"),
        ({"max_records": -1}, "max_records is outside the supported range"),
        (
            {"segment_bytes": MAX_POLICY_VALUE + 1},
            "segment_bytes is outside the supported range",
        ),
    ],
    ids=[
        "empty-segment",
        "headroom-under-one-segment",
        "negative-age",
        "oversized-age",
        "negative-budget",
        "negative-cap",
        "oversized-segment",
    ],
)
def test_an_unusable_bound_is_refused(
    overrides: dict[str, typ.Any], message: str
) -> None:
    """Each contradictory bound is named where it is refused."""
    base: dict[str, typ.Any] = {"segment_bytes": 1, "headroom_bytes": 1}
    policy = RetentionPolicy(**{**base, **overrides})

    with pytest.raises(ValueError, match=message):
        validate_policy(policy, header_floor=header_floor_bytes())


def test_an_age_beyond_the_calendar_is_shortened_to_the_minimum() -> None:
    """An unrepresentable cutoff retains everything rather than raising."""
    oldest = dt.datetime.min.replace(tzinfo=dt.UTC)
    policy = RetentionPolicy(max_age=dt.timedelta(days=MAX_AGE_DAYS))

    assert policy.cutoff(oldest + dt.timedelta(days=1)) == oldest, (
        "an overflowing subtraction clamps to the earliest representable time"
    )


def test_a_naive_time_is_refused() -> None:
    """The cutoff is derived only from an explicitly aware UTC time."""
    policy = RetentionPolicy()

    with pytest.raises(ValueError, match="2026-11-20 00:00:00"):
        policy.cutoff(dt.datetime(2026, 11, 20))  # ruff: ignore[call-datetime-without-tzinfo] - a naive time is what this test must pass in.


def test_no_age_limit_has_no_cutoff() -> None:
    """Disabling age eviction reports no cutoff at all."""
    policy = RetentionPolicy(max_age=dt.timedelta(0))

    assert policy.cutoff(dt.datetime(2026, 11, 20, tzinfo=dt.UTC)) is None, (
        "no age limit means no cutoff"
    )
