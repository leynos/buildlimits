"""Render the retention section of the supervisor status document.

The line names the same values the archive report's ``limits`` document
carries, so an operator comparing status with ``last-run.json`` sees one
policy rather than two summaries that might drift.
"""

import typing as typ

from buildlimits.status_sections import section_of

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ["retention_lines"]

# Each bound is described by the flag that enables it, the value it applies and
# the unit the status line prints, so age and bytes share one formatter.
_BOUNDS = {
    "age": ("age_eviction", "max_age_days", "days", "age"),
    "byte": ("byte_eviction", "max_bytes", "logical bytes", "byte"),
}


def retention_lines(state: cabc.Mapping[str, object]) -> list[str]:
    """Render the effective retention policy and active history.

    Returns
    -------
    list[str]
        One line when the status document carries retention state.

    Examples
    --------
    >>> limits = {
    ...     "max_age_days": 30, "max_age_seconds": 2592000,
    ...     "max_bytes": 15000000000, "max_records": None,
    ...     "headroom_bytes": 25165824, "segment_bytes": 12582912,
    ...     "segmented": True, "age_eviction": True, "byte_eviction": True,
    ... }
    >>> retention_lines({"retention": {"limits": limits}})
    ['cache outcomes retention: 30 days, 15000000000 logical bytes, no record cap']
    """
    section = section_of(state, "retention")
    if section is None:
        return []
    limits = section_of(section, "limits")
    if limits is None:
        return []
    bounds = [
        _bound_text(limits, "age", "no age limit"),
        _bound_text(limits, "byte", "no byte limit"),
        _record_text(limits),
    ]
    return [f"cache outcomes retention: {', '.join(bounds)}{_active_text(section)}"]


def _bound_text(limits: cabc.Mapping[str, object], bound: str, disabled: str) -> str:
    """Describe one quantity bound the policy enforces.

    Parameters
    ----------
    limits : Mapping[str, object]
        The reported limits document.
    bound : str
        Which bound to describe: ``"age"`` or ``"byte"``.
    disabled : str
        The text to use when that bound does not evict.

    Returns
    -------
    str
        The value with its unit, a fallback when the value is missing, or the
        disabled text.
    """
    enabled_key, value_key, unit, kind = _BOUNDS[bound]
    if limits.get(enabled_key) is not True:
        return disabled
    value = limits.get(value_key)
    return f"{value} {unit}" if isinstance(value, int) else f"a {kind} limit"


def _record_text(limits: cabc.Mapping[str, object]) -> str:
    """Describe the record cap the policy enforces.

    Returns
    -------
    str
        ``N record cap`` for an explicit or legacy cap, else ``no record cap``.
    """
    cap = limits.get("max_records")
    return f"{cap} record cap" if isinstance(cap, int) else "no record cap"


def _active_text(section: cabc.Mapping[str, object]) -> str:
    """Describe the active-file snapshot appended to the retention line.

    Returns
    -------
    str
        Active counts and bytes, plus the closed reason when incomplete.
    """
    active = section_of(section, "active")
    if active is None:
        return ""
    text = f"; active {active.get('record_count', 0)} records"
    logical = active.get("logical_bytes")
    if isinstance(logical, int):
        text += f" ({logical} logical bytes)"
    if active.get("complete") is False:
        reason = active.get("reason")
        detail = reason if isinstance(reason, str) else "unknown"
        text += f"; active snapshot incomplete ({detail})"
    return text
