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
    header_floor_bytes,
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
    """The configuration fields the retention rules read.

    The fields are read-only here because ``SupervisorConfig`` is a frozen
    dataclass: a protocol that required writable fields would not accept it,
    and these rules only ever read the settings they are given.
    """

    @property
    def cache_outcomes_max_age_days(self) -> int:
        """Maximum retained age in days."""

    @property
    def cache_outcomes_max_bytes(self) -> int:
        """Maximum retained logical bytes."""

    @property
    def cache_outcomes_max_records(self) -> int:
        """Explicit record cap, or zero when none was configured."""

    @property
    def cache_outcomes_archive_headroom_bytes(self) -> int:
        """Temporary disk headroom reserved for archive work."""

    @property
    def cache_outcomes_archive_preset(self) -> int:
        """The XZ preset used when writing an archive."""


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
        # A positive byte budget below one replacement header can never be met:
        # compaction writes a header whatever it removes, and the supervisor
        # would otherwise disable recording when policy construction failed.
        lambda config: (
            config.cache_outcomes_max_bytes == 0
            or config.cache_outcomes_max_bytes >= header_floor_bytes()
        ),
        "cache_outcomes_max_bytes cannot hold the replacement store header",
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
