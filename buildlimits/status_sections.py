"""Read optional nested sections of the supervisor status document.

Every renderer treats an absent or malformed section the same way: a legacy
document must still render, so a missing mapping simply contributes no line.
Keeping that one rule here means the retention renderer and the main renderer
cannot disagree about what a section is.
"""

import collections.abc as cabc

__all__ = ["section_of"]


def section_of(
    state: cabc.Mapping[str, object], key: str
) -> cabc.Mapping[str, object] | None:
    """Read an optional nested section of the status document.

    Returns
    -------
    Mapping[str, object] | None
        The section, or None when a legacy document omits it or it is not a
        mapping.

    Examples
    --------
    >>> section_of({"retention": {"limits": {}}}, "retention")
    {'limits': {}}
    >>> section_of({}, "retention") is None
    True
    """
    section = state.get(key)
    return section if isinstance(section, cabc.Mapping) else None
