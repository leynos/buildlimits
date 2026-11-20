"""Read the system wall clock and re-export canonical UTC text.

The formatting and parsing helpers live in :mod:`buildlimits.utc_format`, which
performs no clock reads. They are re-exported here because most callers need
the clock and the text together, and a single import keeps their timestamps and
the moment they were taken in agreement.
"""

import datetime as dt

from buildlimits.utc_format import (
    optional_utc_moment as optional_utc_moment,
)
from buildlimits.utc_format import (
    optional_utc_text as optional_utc_text,
)
from buildlimits.utc_format import (
    utc_moment as utc_moment,
)
from buildlimits.utc_format import (
    utc_text as utc_text,
)


def utc_now() -> dt.datetime:
    """Read the system wall clock as an aware UTC value.

    Returns
    -------
    datetime
        The current aware UTC wall-clock value.
    """
    return dt.datetime.now(dt.UTC)
