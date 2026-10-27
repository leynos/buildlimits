"""What a configured cache mode says without a backend object in hand.

Some readers hold a mode string but no backend: the frozen readings the
recovery trigger judges, the status document and renderer, and the drain
wording. Those values cross a boundary, so the facts they need are projected
into :class:`ModeFacts` here rather than each reader re-deriving them from the
string.

This module is deliberately free of imports from the rest of the package. The
mode table is configuration vocabulary, not a port, so it must not depend on
the protocol it describes.

Examples
--------
>>> is_cache("direct"), is_cache("sccache")
(False, True)
>>> is_detached_mode("kache"), is_detached_mode("sccache")
(False, True)
>>> mode_name("kache"), mode_unavailable("kache")
('kache', 'unavailable')
"""

import typing as typ


class ModeFacts(typ.NamedTuple):
    """What the configured-mode string alone can say about a backend.

    Attributes
    ----------
    name : str
        The backend's name, for the journal and status wording.
    in_scope_work : bool
        Whether every compilation the backend accepts runs inside the
        compiler group's own scope.
    cached : bool
        Whether the mode asks for a cache at all. It is independent of
        ``in_scope_work``: ``direct`` is in scope and caches nothing, while a
        future in-scope cache would be both in scope and cached.
    unavailable : str
        How status words this backend when it cannot serve compilations.
    """

    name: str
    in_scope_work: bool
    cached: bool
    unavailable: str


# One row per configured mode. This table is the only place a mode string is
# read; every other question is answered from a backend object or from the
# :class:`ModeFacts` projected out of this row. A mode absent from the table
# was never configured, so it is treated as ``direct``.
#
# sccache is probed by connecting to its socket, so it reads as *unreachable*;
# kache is probed through its own client, which also fails for a missing
# executable or an unusable store, so it reads as *unavailable*. The word is
# part of each row rather than derived, because it is the backend's own
# description of what its absence means.
MODES = {
    "direct": ModeFacts("direct", in_scope_work=True, cached=False, unavailable=""),
    "sccache": ModeFacts(
        "sccache", in_scope_work=False, cached=True, unavailable="unreachable"
    ),
    "kache": ModeFacts(
        "kache", in_scope_work=True, cached=True, unavailable="unavailable"
    ),
}

CACHE_MODES = frozenset(mode for mode, facts in MODES.items() if facts.cached)


def facts(mode: str) -> ModeFacts:
    """Return the row for a configured mode, or ``direct`` for an unknown one.

    Parameters
    ----------
    mode : str
        A configured cache mode.

    Returns
    -------
    ModeFacts
        The facts projected from the configured mode.

    Examples
    --------
    >>> facts("kache").name
    'kache'
    >>> facts("never-configured").name
    'direct'
    """
    return MODES.get(mode, MODES["direct"])


def is_cache(mode: str) -> bool:
    """Report whether a configured cache mode names a cache.

    Parameters
    ----------
    mode : str
        A configured cache mode.

    Returns
    -------
    bool
        ``True`` for a mode composed with a cache, ``False`` for ``direct``.

    Examples
    --------
    >>> is_cache("sccache"), is_cache("direct")
    (True, False)
    """
    return mode in CACHE_MODES


def mode_name(mode: str) -> str:
    """Return the backend name that wording for a configured mode uses.

    Parameters
    ----------
    mode : str
        A configured cache mode.

    Returns
    -------
    str
        The backend's name, or ``direct`` when the mode names no cache.

    Examples
    --------
    >>> mode_name("sccache"), mode_name("direct")
    ('sccache', 'direct')
    """
    return facts(mode).name


def mode_unavailable(mode: str) -> str:
    """Return the word status uses for a backend that cannot serve.

    Parameters
    ----------
    mode : str
        A configured cache mode.

    Returns
    -------
    str
        The backend's own word, or an empty string for a mode that names no
        cache and so never renders an availability phrase.

    Examples
    --------
    >>> mode_unavailable("sccache"), mode_unavailable("kache")
    ('unreachable', 'unavailable')
    """
    return facts(mode).unavailable


def is_detached_mode(mode: str) -> bool:
    """Report whether a configured cache mode may leave work outside a scope.

    Used where only the configured mode is available -- the frozen readings
    the recovery trigger judges, and the status document -- because those
    values cross a boundary without carrying the backend object.

    Parameters
    ----------
    mode : str
        A configured cache mode.

    Returns
    -------
    bool
        ``True`` only for a mode whose backend is detached.

    Examples
    --------
    >>> is_detached_mode("sccache"), is_detached_mode("direct")
    (True, False)
    """
    return not facts(mode).in_scope_work
