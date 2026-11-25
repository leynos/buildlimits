"""UTC timestamp encoding and decoding for cache-outcome store values.

The store, its archives and its snapshot readers all name moments in the same
form. Keeping the canonical writer and the strict reader together means a
value the encoder accepts is the value the decoder reads back, and a malformed
or non-UTC value is a closed failure rather than a local-time guess.
"""

import dataclasses as dc
import datetime as dt


@dc.dataclass(frozen=True, slots=True)
class _Invalid:
    """Sentinel separating a malformed optional timestamp from an absent one."""


INVALID_TIMESTAMP = _Invalid()
"""A present timestamp value that is not a supported UTC moment."""


def timestamp_text(value: dt.datetime) -> str:
    """Serialize an explicit UTC timestamp in canonical ISO form.

    Returns
    -------
    str
        The timestamp with a trailing ``Z`` offset.

    Raises
    ------
    ValueError
        If the timestamp is naive or has a non-UTC offset.

    Examples
    --------
    >>> timestamp_text(dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    '2026-01-01T00:00:00Z'
    """
    if value.tzinfo is None or value.utcoffset() != dt.timedelta(0):
        msg = "a timestamp to serialize must be UTC"
        raise ValueError(msg)
    return value.isoformat().replace("+00:00", "Z")


def parse_timestamp(value: object) -> dt.datetime | None:
    """Parse an explicit UTC ISO timestamp.

    Returns
    -------
    datetime or None
        A UTC timestamp, or no value for malformed or non-UTC input.

    Examples
    --------
    >>> parse_timestamp("2026-01-01T00:00:00Z")
    datetime.datetime(2026, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)
    >>> parse_timestamp("2026-01-01T00:00:00") is None
    True
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        return None
    return parsed.astimezone(dt.UTC)


def optional_timestamp(value: object) -> dt.datetime | _Invalid | None:
    """Parse an optional UTC timestamp, distinguishing absence from corruption.

    Returns
    -------
    datetime | _Invalid | None
        The parsed time, the invalid sentinel for a present value that is not
        a UTC timestamp, or None for an explicit null or an absent field.

    Examples
    --------
    >>> optional_timestamp(None) is None
    True
    >>> optional_timestamp("not-a-time") is INVALID_TIMESTAMP
    True
    """
    if value is None:
        return None
    parsed = parse_timestamp(value)
    return INVALID_TIMESTAMP if parsed is None else parsed


__all__ = [
    "INVALID_TIMESTAMP",
    "optional_timestamp",
    "parse_timestamp",
    "timestamp_text",
]
