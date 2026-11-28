"""Render the optional cache-outcome recording section of the status summary.

The line carries only bounded counts drawn from closed vocabularies: nothing
here names an attempt, a generation, a path or a cache key. Keeping the section
apart from the rest of the summary lets its counters grow without crowding the
status renderer.
"""

import typing as typ

from buildlimits.status_sections import section_of

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_SNAPSHOT_UNAVAILABLE = "snapshot_unavailable"
_SNAPSHOT_UNAVAILABLE_LINE = "cache outcomes status unavailable"

__all__ = ["outcome_lines"]


def outcome_lines(state: cabc.Mapping[str, object]) -> list[str]:
    """Render the optional outcome-recording state without private values.

    Returns
    -------
    list[str]
        The fixed optional section, or an empty list when absent.
    """
    section = section_of(state, "cache_outcomes")
    if section is None:
        return []
    if section.get("enabled") is not True:
        if section.get("reason") == _SNAPSHOT_UNAVAILABLE:
            return [_SNAPSHOT_UNAVAILABLE_LINE]
        return [_disabled_line(section)]
    line = (
        "cache outcomes recording: "
        f"{section.get('attributed', 0)} attributed, "
        f"{section.get('readings', 0)} readings"
    )
    line += _counted_clauses(section)
    reason = section.get("last_reason")
    return [line if not isinstance(reason, str) else f"{line}; last reason {reason}"]


def _counted_clauses(section: cabc.Mapping[str, object]) -> str:
    """Return the phrase for each non-zero count the recorder publishes.

    A zero count is omitted, so a healthy line stays as short as it was before
    the counters existed.

    Returns
    -------
    str
        The clauses, each starting with a comma, or the empty string.
    """
    clauses = _omission_clause(section)
    counts = (
        ("offered_uncorrelated_rows", "offered without a correlation pair"),
        ("offered_repeated_attempts", "offered repeating an attempt identity"),
    )
    for name, description in counts:
        value = section.get(name, 0)
        if isinstance(value, int) and value:
            clauses += f", {value} {description}"
    return clauses


def _omission_clause(section: cabc.Mapping[str, object]) -> str:
    """Return the omission clause, or the empty string when none were omitted.

    Returns
    -------
    str
        The count and its two closed reasons.
    """
    omitted = section.get("record_omitted", 0)
    if not isinstance(omitted, int) or not omitted:
        return ""
    return (
        f", {omitted} omitted "
        f"({section.get('record_omitted_key', 0)} without a key, "
        f"{section.get('record_omitted_label_time', 0)} without a label time)"
    )


def _disabled_line(section: cabc.Mapping[str, object]) -> str:
    """Say why recording stopped, without inventing a reason.

    The renderer appends ``last_reason`` only when the supplied snapshot holds
    a string. Construction failure and a shell fault after startup both publish
    the fixed disabled description, so no earlier reading reason survives those
    paths.

    Returns
    -------
    str
        The disabled headline, with the last closed reason when reported.
    """
    headline = "cache outcomes disabled after an internal failure"
    reason = section.get("last_reason")
    return (
        headline if not isinstance(reason, str) else f"{headline}; last reason {reason}"
    )
