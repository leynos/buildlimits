"""Bounded JSON Lines scanning for the cache-outcome store.

The store is newline-delimited JSON, so every decoder first has to turn a
bounded document, or one raw row, into a parsed value without trusting the
bytes. That scanning is the same whatever kind of record follows it, so it
lives here rather than inside the record decoder.
"""

import json

from buildlimits.outcome_record_format import MAX_RECORD_BYTES, MAX_STORE_BYTES

__all__ = [
    "_UNPARSABLE",
    "_Unparsable",
    "_bounded_lines",
    "_parse_json_lines",
    "_parsed_line",
    "_raw_json_row",
]


class _Unparsable:
    """Sentinel distinguishing an unreadable line from a parsed JSON value."""


_UNPARSABLE = _Unparsable()


def _bounded_lines(document: str) -> tuple[list[str], bool] | None:
    """Split a bounded document into candidate rows.

    Returns
    -------
    tuple[list[str], bool] or None
        Rows without their terminator and whether the document was terminated,
        or ``None`` when the document is empty or oversized.

    Examples
    --------
    >>> _bounded_lines('{"a":1}' + chr(10))
    (['{"a":1}'], True)
    >>> _bounded_lines("") is None
    True
    """
    if len(document.encode("utf-8")) > MAX_STORE_BYTES:
        return None
    terminated = document.endswith("\n")
    lines = document.split("\n")
    if terminated:
        lines.pop()
    if not lines or not lines[0]:
        return None
    return lines, terminated


def _parsed_line(line: str) -> object | _Unparsable:
    """Parse one bounded row, or report that it is not a row.

    Returns
    -------
    object or _Unparsable
        The decoded JSON value, or the unparsable sentinel.

    Examples
    --------
    >>> _parsed_line('{"a":1}')
    {'a': 1}
    >>> _parsed_line("") is _UNPARSABLE
    True
    """
    if not line or len(line.encode("utf-8")) > MAX_RECORD_BYTES:
        return _UNPARSABLE
    try:
        return json.loads(line, parse_constant=lambda _value: None)
    except ValueError, TypeError, RecursionError:
        return _UNPARSABLE


def _raw_json_row(line: bytes) -> object | _Unparsable:
    """Parse one bounded raw row, or report that it is not a row.

    Returns
    -------
    object or _Unparsable
        The decoded JSON value, or the unparsable sentinel.

    Examples
    --------
    >>> _raw_json_row(b'{"a":1}')
    {'a': 1}
    >>> _raw_json_row(b"") is _UNPARSABLE
    True
    """
    if not line or len(line) > MAX_RECORD_BYTES:
        return _UNPARSABLE
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        return _UNPARSABLE
    try:
        return json.loads(text, parse_constant=lambda _value: None)
    except ValueError, TypeError, RecursionError:
        return _UNPARSABLE


def _parse_json_lines(document: str) -> tuple[object, ...] | None:
    """Parse bounded rows and drop a malformed final append fragment.

    Returns
    -------
    tuple[object, ...] or None
        Parsed rows, or no value for an invalid or oversized document.
    """
    split = _bounded_lines(document)
    if split is None:
        return None
    lines, terminated = split
    parsed: list[object] = []
    for index, line in enumerate(lines):
        value = _parsed_line(line)
        if not isinstance(value, _Unparsable):
            parsed.append(value)
        elif index == len(lines) - 1 and not terminated:
            break
        else:
            return None
    return tuple(parsed)
