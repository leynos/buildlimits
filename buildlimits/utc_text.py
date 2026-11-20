"""Canonical UTC text for the timestamps reports and snapshots carry.

Both the outcome-store reports and the prediction snapshot state observation
times as ISO 8601 in UTC with a trailing ``Z``. Keeping one implementation
means a stored report and a stored snapshot read the same way in a diff, and
one parser reads either back into the moment it names. The same module reads
the system wall clock as UTC, so a default clock and the text it produces
cannot disagree about the time zone.
"""

import datetime as dt


def utc_now() -> dt.datetime:
    """Read the system wall clock as an aware UTC value.

    Returns
    -------
    datetime
        The current aware UTC wall-clock value.
    """
    return dt.datetime.now(dt.UTC)


def utc_text(value: dt.datetime) -> str:
    """Serialize one timestamp as ISO 8601 text in UTC.

    Returns
    -------
    str
        The time converted to UTC, with ``+00:00`` written as ``Z``.

    Raises
    ------
    ValueError
        If the value carries no offset. ``astimezone`` would read such a value
        as the host's local time, so the same instant would be written
        differently on two machines and a stored snapshot would stop being
        reproducible. An absent offset is a caller's mistake, not a moment to
        guess at.

    Examples
    --------
    >>> from datetime import UTC, datetime, timedelta, timezone
    >>> utc_text(datetime(2026, 11, 17, 12, 30, tzinfo=UTC))
    '2026-11-17T12:30:00Z'
    >>> utc_text(datetime(2026, 11, 17, 13, 30, tzinfo=timezone(timedelta(hours=1))))
    '2026-11-17T12:30:00Z'
    """
    if value.tzinfo is None:
        msg = "a timestamp to serialize must carry an offset"
        raise ValueError(msg)
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")


def optional_utc_text(value: dt.datetime | None) -> str | None:
    """Serialize one optional timestamp, keeping an unknown time as null.

    The snapshot records admission times and journal spans that may be
    unknown, so an absent time is written as an explicit null rather than an
    empty string or an invented moment.

    Returns
    -------
    str | None
        Canonical ISO text, or None when no time is known. A present value
        with no offset is refused by :func:`utc_text`.

    Examples
    --------
    >>> optional_utc_text(None) is None
    True
    >>> optional_utc_text(dt.datetime(2026, 11, 17, 12, 30, tzinfo=dt.UTC))
    '2026-11-17T12:30:00Z'
    """
    return None if value is None else utc_text(value)


def optional_utc_moment(
    value: object, *, label: str, message: str
) -> dt.datetime | None:
    """Read one optional timestamp, refusing a present value that is not text.

    Parameters
    ----------
    value : object
        The decoded field value, of any shape.
    label : str
        What the caller calls the field, used to name it in a parse error.
    message : str
        The error message for a present value that is not text.

    Returns
    -------
    datetime | None
        The moment in UTC, or None for an explicit null.

    Raises
    ------
    TypeError
        If the value is neither text nor null.

    Examples
    --------
    >>> optional_utc_moment(None, label="a time", message="a time must be text")
    >>> optional_utc_moment(
    ...     "2026-11-17T12:30:00Z", label="a time", message="a time must be text"
    ... ).isoformat()
    '2026-11-17T12:30:00+00:00'
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(message)
    return utc_moment(value, label=label)


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
