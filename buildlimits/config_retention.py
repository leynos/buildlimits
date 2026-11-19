"""Retention settings that only mean something while outcomes are recorded.

The keys live with the supervisor configuration, but their rules are about
history rather than admission. Keeping them here lets the archive command and
the supervisor share one definition of what a valid retention policy is.
"""

import typing as typ

from buildlimits.outcome_retention import (
    MAX_AGE_DAYS,
    MAX_POLICY_VALUE,
    MAX_XZ_PRESET,
    MIN_XZ_PRESET,
    SEGMENT_BYTES,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# Retention settings describe a history that only exists while recording is
# on. Accepting one with ``cache_outcomes = "off"`` would look configured and
# silently do nothing, so a written retention key requires record mode.
RETENTION_KEYS = frozenset({
    "cache_outcomes_max_age_days",
    "cache_outcomes_max_bytes",
    "cache_outcomes_max_records",
    "cache_outcomes_archive_headroom_bytes",
    "cache_outcomes_archive_preset",
})


class RetentionSettings(typ.Protocol):
    """The configuration fields the retention rules read."""

    cache_outcomes_max_age_days: int
    cache_outcomes_max_bytes: int
    cache_outcomes_max_records: int
    cache_outcomes_archive_headroom_bytes: int
    cache_outcomes_archive_preset: int


def reject_written_keys(values: cabc.Mapping[str, object]) -> str | None:
    """Return the error for a retention key written with recording off.

    Parameters
    ----------
    values : Mapping[str, object]
        The converted settings, keyed as written in the document.

    Returns
    -------
    str | None
        The message to raise, or None when the keys are acceptable.
    """
    if values.get("cache_outcomes", "off") == "record":
        return None
    written = RETENTION_KEYS & values.keys()
    if not written:
        return None
    return f"setting {min(written)!r} is only valid when cache_outcomes is 'record'"


RETENTION_RULES: tuple[tuple[cabc.Callable[[RetentionSettings], bool], str], ...] = (
    (
        lambda config: 0 <= config.cache_outcomes_max_age_days <= MAX_AGE_DAYS,
        f"cache_outcomes_max_age_days must be between 0 and {MAX_AGE_DAYS}",
    ),
    (
        lambda config: 0 <= config.cache_outcomes_max_bytes <= MAX_POLICY_VALUE,
        "cache_outcomes_max_bytes is outside the supported range",
    ),
    (
        lambda config: 0 <= config.cache_outcomes_max_records <= MAX_POLICY_VALUE,
        "cache_outcomes_max_records is outside the supported range",
    ),
    (
        lambda config: (
            SEGMENT_BYTES
            <= config.cache_outcomes_archive_headroom_bytes
            <= MAX_POLICY_VALUE
        ),
        "cache_outcomes_archive_headroom_bytes must hold at least one segment",
    ),
    (
        lambda config: (
            MIN_XZ_PRESET <= config.cache_outcomes_archive_preset <= MAX_XZ_PRESET
        ),
        "cache_outcomes_archive_preset must be between 0 and 9",
    ),
)
