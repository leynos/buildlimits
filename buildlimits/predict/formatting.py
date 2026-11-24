"""Small value formatters shared by the report and latency tables.

A value that is not known is rendered as a dash everywhere, so a missing
measurement is never mistaken for a zero.
"""

import collections.abc as cabc
import typing as typ

MISSING = "-"

TRUNCATED_OBSERVER_NOTE = (
    "Observer counts cover only the retained journal window; the query "
    "reached its record cap."
)
"""The one sentence both reports print when a journal query was truncated.

The plain command report and the Markdown report describe the same partial
window, so the warning is stored once rather than reworded at each boundary.
"""

_GENERATION_UNAVAILABLE = (
    "by generation: unavailable (a store row's key names its sccache version, "
    "not the server generation that produced it, so no per-generation label "
    "breakdown can be measured)"
)


def generation_unavailable_line() -> str:
    """Return the honest statement of the dimension this history cannot carry.

    Issue #68 asks for label loss by sccache generation. A store row's key
    carries the server *version*, never the generation the recorder observed
    when it read a window, and adding one would be a new row field for a
    measurement no report can reconcile. The dimension is therefore named as
    unavailable with its reason rather than omitted or filled with a count the
    history cannot support.

    Returns
    -------
    str
        The fixed sentence both reports print for the missing dimension.

    Examples
    --------
    >>> generation_unavailable_line().startswith("by generation: unavailable")
    True
    """
    return _GENERATION_UNAVAILABLE


def as_score(value: object) -> str:
    """Format a score or rate as four decimals.

    Returns
    -------
    str
        Four decimals, or a dash when absent.

    Examples
    --------
    >>> as_score(0.123456)
    '0.1235'
    >>> as_score(None)
    '-'
    """
    return MISSING if value is None else f"{typ.cast('float', value):.4f}"


def as_count(value: object) -> str:
    """Format a count with thousands separators.

    Returns
    -------
    str
        The count, or a dash when absent.

    Examples
    --------
    >>> as_count(1234)
    '1,234'
    >>> as_count(None)
    '-'
    """
    return MISSING if value is None else f"{typ.cast('int', value):,}"


def as_percent(value: object) -> str:
    """Format a fraction as a percentage.

    Returns
    -------
    str
        One decimal place, or a dash when absent.

    Examples
    --------
    >>> as_percent(0.256)
    '25.6%'
    >>> as_percent(None)
    '-'
    """
    return MISSING if value is None else f"{100 * typ.cast('float', value):.1f}%"


def admission_line(stats: cabc.Mapping[str, object]) -> str:
    """Return the admission-time coverage bullet shared by both reports.

    Returns
    -------
    str
        The recorded, usable, missing and late-admission counts in one line.

    Examples
    --------
    >>> admission_line({"admission_recorded": 2, "admission_usable": 2,
    ...                 "admission_missing": 0,
    ...                 "admission_unusable": 0})  # doctest: +ELLIPSIS
    '- Admission times: 2 recorded, 2 usable for a prediction, ...'
    """
    return (
        f"- Admission times: {as_count(stats['admission_recorded'])} recorded, "
        f"{as_count(stats['admission_usable'])} usable for a prediction, "
        f"{as_count(stats['admission_missing'])} missing, "
        f"{as_count(stats['admission_unusable'])} recorded after their own label"
    )


def as_counts(value: object) -> str:
    """Format a closed-vocabulary count map as stable key/count pairs.

    Returns
    -------
    str
        Sorted ``key=count`` pairs, or ``none`` for an empty map.

    Examples
    --------
    >>> as_counts({"miss": 2, "hit": 10})
    'hit=10, miss=2'
    >>> from types import MappingProxyType
    >>> as_counts(MappingProxyType({"hit": 3}))
    'hit=3'
    >>> as_counts({})
    'none'
    """
    if not isinstance(value, cabc.Mapping):
        return MISSING
    counts = typ.cast("cabc.Mapping[str, object]", value)
    return (
        ", ".join(f"{name}={as_count(count)}" for name, count in sorted(counts.items()))
        or "none"
    )
