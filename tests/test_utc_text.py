"""Canonical UTC text names one moment, whatever host writes it."""

import datetime as dt

import pytest

from buildlimits.utc_text import optional_utc_text, utc_text


def test_a_naive_timestamp_is_refused_rather_than_read_as_local() -> None:
    """A value with no offset would be written as this host's local time.

    ``astimezone`` reads a naive value as local, so the same instant would be
    written differently on two machines and a stored snapshot would stop being
    reproducible. The missing offset is reported instead of guessed at.
    """
    naive = dt.datetime.fromisoformat("2026-11-17T12:30:00")

    with pytest.raises(ValueError, match="must carry an offset"):
        utc_text(naive)


def test_an_offset_timestamp_is_written_in_utc() -> None:
    """An aware value keeps the instant it names, expressed as UTC."""
    moment = dt.datetime(
        2026, 11, 17, 13, 30, tzinfo=dt.timezone(dt.timedelta(hours=1))
    )

    assert utc_text(moment) == "2026-11-17T12:30:00Z", "the same instant"


def test_an_absent_optional_timestamp_stays_null() -> None:
    """No time is written as null, not as an empty string."""
    assert optional_utc_text(None) is None, "an unknown time is null, not text"
