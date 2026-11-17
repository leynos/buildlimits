"""Canonical UTC text for the timestamps reports and snapshots carry.

Both the outcome-store reports and the prediction snapshot state observation
times as ISO 8601 in UTC with a trailing ``Z``. Keeping one implementation
means a stored report and a stored snapshot read the same way in a diff.
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
