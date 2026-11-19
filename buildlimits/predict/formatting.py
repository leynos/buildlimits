"""Small value formatters shared by the report and latency tables.

A value that is not known is rendered as a dash everywhere, so a missing
measurement is never mistaken for a zero.
"""

import typing as typ

MISSING = "-"


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
