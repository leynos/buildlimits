"""Canonical UTC text for the timestamps reports and snapshots carry.

Both the outcome-store reports and the prediction snapshot state observation
times as ISO 8601 in UTC with a trailing ``Z``. Keeping one implementation
means a stored report and a stored snapshot read the same way in a diff, and
one parser reads either back into the moment it names.
"""

import datetime as dt


def utc_text(value: dt.datetime) -> str:
    """Serialize one timestamp as ISO 8601 text in UTC.

    Returns
    -------
    str
        The time converted to UTC, with ``+00:00`` written as ``Z``.

    Examples
    --------
    >>> from datetime import UTC, datetime, timedelta, timezone
    >>> utc_text(datetime(2026, 11, 17, 12, 30, tzinfo=UTC))
    '2026-11-17T12:30:00Z'
    >>> utc_text(datetime(2026, 11, 17, 13, 30, tzinfo=timezone(timedelta(hours=1))))
    '2026-11-17T12:30:00Z'
    """
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def utc_moment(text: str, *, label: str) -> dt.datetime:
    """Read one moment from offset-bearing ISO 8601 text.

    Both snapshot readers accept the same timestamps, so they share one
    parser: an offset is required because a local time is ambiguous, and an
    offset that would push the moment outside the range a UTC datetime can
    hold is a bad field value rather than a crash.

    Parameters
    ----------
    text : str
        The timestamp text to read.
    label : str
        What the caller calls the field, used to name it in an error, such as
        ``"a snapshot timestamp"``.

    Returns
    -------
    datetime
        The moment in UTC.

    Raises
    ------
    ValueError
        If the text is not ISO 8601, carries no offset, or its offset moves it
        outside the range a UTC datetime can hold.

    Examples
    --------
    >>> utc_moment("2026-11-17T13:30:00+01:00", label="a timestamp").isoformat()
    '2026-11-17T12:30:00+00:00'
    """
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as error:
        msg = f"{label} must be ISO 8601"
        raise ValueError(msg) from error
    if parsed.tzinfo is None:
        msg = f"{label} must carry an offset"
        raise ValueError(msg)
    try:
        return parsed.astimezone(dt.UTC)
    except OverflowError as error:
        # The earliest date plus a positive offset has no UTC equivalent:
        # 0001-01-01T00:00:00+01:00 would need year zero, a bad field value.
        msg = f"{label} must convert to UTC"
        raise ValueError(msg) from error
