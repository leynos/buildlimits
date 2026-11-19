"""Validated cache-outcome retention policy shared by writer and archiver.

One policy object answers every retention question: how old history may be,
how many logical uncompressed bytes may be retained, whether an independent
record cap applies, and how much temporary disk headroom an archive operation
may reserve. The writer, the archiver and the read-only history view all use
this object so they cannot disagree about a bound.

The byte budget counts *logical uncompressed* bytes of retained history.
Archived segments are counted from their sidecar's logical size, and active and
sealed files from ``stat``. Temporary archive files are covered by
``headroom_bytes`` and never count against the retained budget.
"""

import dataclasses as dc
import datetime as dt
import typing as typ

SEGMENT_BYTES = 12 * 1024 * 1024
"""The active file's high-water mark before the writer seals it."""

LEGACY_MAX_RECORDS = 10_000
"""The record cap kept for configurations that do not opt into a policy."""

LEGACY_MAX_BYTES = SEGMENT_BYTES
"""The legacy compaction target, retained as the default byte budget."""

DEFAULT_MAX_AGE_DAYS = 30
DEFAULT_HEADROOM_BYTES = 2 * SEGMENT_BYTES
DAY_SECONDS = 86_400
MAX_AGE_DAYS = 365_000
MAX_POLICY_VALUE = 2**63 - 1
MIN_XZ_PRESET = 0
MAX_XZ_PRESET = 9

__all__ = [
    "DAY_SECONDS",
    "DEFAULT_HEADROOM_BYTES",
    "DEFAULT_MAX_AGE_DAYS",
    "LEGACY_MAX_BYTES",
    "LEGACY_MAX_RECORDS",
    "MAX_AGE_DAYS",
    "MAX_POLICY_VALUE",
    "MAX_XZ_PRESET",
    "MIN_XZ_PRESET",
    "SEGMENT_BYTES",
    "RetentionPolicy",
    "policy_from_settings",
    "validate_policy",
]


@dc.dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """The effective retention bounds and the active segment size.

    Attributes
    ----------
    max_age : datetime.timedelta
        Oldest retained observation age. Zero disables age eviction.
    max_bytes : int
        Logical uncompressed bytes retained across all history states. Zero
        disables the byte bound.
    max_records : int | None
        An explicitly configured record cap, or ``None`` for no count cap.
    headroom_bytes : int
        Temporary disk space an archive run may use outside ``max_bytes``.
    segment_bytes : int
        The active file high-water mark; also the maximum source size a trim
        or archive pass must stream through.

    Examples
    --------
    >>> RetentionPolicy().is_segmented
    False
    >>> RetentionPolicy(max_bytes=15_000_000_000).is_segmented
    True
    """

    max_age: dt.timedelta = dt.timedelta(days=DEFAULT_MAX_AGE_DAYS)
    max_bytes: int = LEGACY_MAX_BYTES
    max_records: int | None = LEGACY_MAX_RECORDS
    headroom_bytes: int = DEFAULT_HEADROOM_BYTES
    segment_bytes: int = SEGMENT_BYTES

    @property
    def has_age_limit(self) -> bool:
        """Whether history is evicted by age.

        Returns
        -------
        bool
            Whether ``max_age`` is positive.
        """
        return self.max_age > dt.timedelta(0)

    @property
    def has_byte_limit(self) -> bool:
        """Whether history is evicted by logical bytes.

        Returns
        -------
        bool
            Whether ``max_bytes`` is positive.
        """
        return self.max_bytes > 0

    @property
    def is_segmented(self) -> bool:
        """Whether closed history is sealed instead of compacted in place.

        A byte budget larger than one active segment cannot be enforced by
        rewriting the active document, so the store must rotate. An unlimited
        byte budget also requires rotation, because no active file may grow
        without bound.

        Returns
        -------
        bool
            Whether the writer seals closed segments.
        """
        return not self.has_byte_limit or self.max_bytes > self.segment_bytes

    def cutoff(self, now: dt.datetime) -> dt.datetime | None:
        """Return the oldest observation time retained by age.

        Parameters
        ----------
        now : datetime
            Caller-supplied aware UTC wall time.

        Returns
        -------
        datetime | None
            The cutoff, or ``None`` when age eviction is disabled.
        """
        _require_utc(now)
        if not self.has_age_limit:
            return None
        try:
            return now - self.max_age
        except OverflowError:
            return dt.datetime.min.replace(tzinfo=dt.UTC)

    def exceeds(self, *, logical_bytes: int, records: int) -> bool:
        """Check whether retained history exceeds either configured bound.

        Parameters
        ----------
        logical_bytes : int
            Logical uncompressed bytes of retained closed and active history.
        records : int
            Retained record count, excluding headers.

        Returns
        -------
        bool
            Whether a byte or explicit record bound is exceeded.

        Examples
        --------
        >>> RetentionPolicy(max_bytes=10, max_records=None).exceeds(
        ...     logical_bytes=11, records=1
        ... )
        True
        """
        too_large = self.has_byte_limit and logical_bytes > self.max_bytes
        too_many = self.max_records is not None and records > self.max_records
        return too_large or too_many


class _RetentionSettings(typ.Protocol):
    """The configuration attributes a policy is derived from."""

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


def policy_from_settings(settings: _RetentionSettings) -> RetentionPolicy:
    """Build the effective policy from validated supervisor settings.

    A byte budget larger than one active segment switches the store to
    segmented mode. In that mode an omitted record cap means "no cap"; in
    legacy mode an omitted cap keeps the historical 10,000-row bound.

    Parameters
    ----------
    settings : _RetentionSettings
        The validated configuration values.

    Returns
    -------
    RetentionPolicy
        The effective policy.

    Examples
    --------
    >>> class Settings:
    ...     cache_outcomes_max_age_days = 30
    ...     cache_outcomes_max_bytes = 15_000_000_000
    ...     cache_outcomes_max_records = 0
    ...     cache_outcomes_archive_headroom_bytes = 64 * 1024 * 1024
    >>> policy = policy_from_settings(Settings())
    >>> policy.is_segmented, policy.max_records
    (True, None)
    """
    max_bytes = settings.cache_outcomes_max_bytes
    segmented = max_bytes == 0 or max_bytes > SEGMENT_BYTES
    explicit_cap = settings.cache_outcomes_max_records
    if explicit_cap > 0:
        max_records = explicit_cap
    elif segmented:
        max_records = None
    else:
        max_records = LEGACY_MAX_RECORDS
    policy = RetentionPolicy(
        max_age=dt.timedelta(days=settings.cache_outcomes_max_age_days),
        max_bytes=max_bytes,
        max_records=max_records,
        headroom_bytes=settings.cache_outcomes_archive_headroom_bytes,
    )
    validate_policy(policy)
    return policy


def validate_policy(policy: RetentionPolicy) -> None:
    """Reject a policy whose bounds are contradictory or unusable.

    Parameters
    ----------
    policy : RetentionPolicy
        The policy to check.

    Raises
    ------
    ValueError
        If a bound is negative, overflows the policy range, or the temporary
        headroom cannot hold one maximal segment.
    """
    _require_range("max_bytes", policy.max_bytes)
    _require_range("headroom_bytes", policy.headroom_bytes)
    _require_range("segment_bytes", policy.segment_bytes)
    if policy.segment_bytes <= 0:
        msg = "segment_bytes must be positive"
        raise ValueError(msg)
    if policy.headroom_bytes < policy.segment_bytes:
        msg = "headroom_bytes must hold at least one segment"
        raise ValueError(msg)
    if policy.max_records is not None:
        _require_range("max_records", policy.max_records)
    if policy.max_age < dt.timedelta(0):
        msg = "max_age must not be negative"
        raise ValueError(msg)
    if policy.max_age > dt.timedelta(days=MAX_AGE_DAYS):
        msg = "max_age is too large"
        raise ValueError(msg)


def _require_range(name: str, value: int) -> None:
    """Require one non-negative integer within the policy range.

    Raises
    ------
    ValueError
        If the value is negative or exceeds ``MAX_POLICY_VALUE``.
    """
    if value < 0 or value > MAX_POLICY_VALUE:
        msg = f"{name} is outside the supported range"
        raise ValueError(msg)


def _require_utc(value: dt.datetime) -> None:
    """Require explicit UTC time values; never read the system clock.

    Raises
    ------
    ValueError
        If the value is naive or has a non-UTC offset.
    """
    if value.tzinfo is None or value.utcoffset() != dt.timedelta(0):
        raise ValueError(value)
