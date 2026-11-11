"""Decode the checker's error graph into the domain's links and traces.

An error graph is the checker's own document, and its keys — ``Type``,
``Name``, ``Node.state`` — name nothing in the domain. Decoding them belongs in
an adapter for exactly that reason: :mod:`modelcheck.trace` decides what a path
*means* and must not depend on a format the checker is free to change.

Every shape that does not name a link is refused rather than guessed at. The
graph is untrusted input read from a file the checker wrote, so a scalar
document, a link that is not a mapping, a transition with no label and a link
with no state mapping all raise from here rather than becoming a trace this
module invented.

The vocabulary rule in ``tests/test_modelcheck_layering.py`` keeps these keys
out of every pure module. This module is the boundary they are allowed to
cross.
"""

import collections.abc as cabc
import json
import typing as typ

from modelcheck.trace import Link, LinkKind, Trace, normalize_trace

__all__ = ["links", "trace"]
"""``links`` decodes the document, and ``trace`` decodes and normalizes it.

A caller that has an error graph as text wants :func:`trace`; one that already
holds the parsed document, or is testing the decoding on its own, wants
:func:`links`.
"""


def links(entries: object) -> tuple[Link, ...]:
    """Decode one ``error-graph.json`` document into domain links.

    The checker's keys are decoded here, in the adapter, so that
    :func:`modelcheck.trace.normalize_trace` can work on values that carry no
    checker vocabulary. Every shape that does not name a link is refused
    rather than guessed at: this is untrusted input read from a file.

    Parameters
    ----------
    entries : object
        The document as :func:`json.loads` returned it. It is typed loosely
        because it is the checker's file, and the checker is not a caller this
        module can hold to a shape.

    Returns
    -------
    tuple[Link, ...]
        One link per recorded entry, in the order the checker wrote them.

    A malformed entry raises ``ValueError`` from the per-link decoder rather
    than from this function, so it is described here rather than under
    ``Raises``: a first entry that names an action, a transition with no name,
    and an entry with no ``Node.state`` mapping are all refused there.

    Raises
    ------
    TypeError
        If the document is not a list of mappings. A scalar or a string is
        not an error graph, and indexing it would leak the wrong failure.

    Examples
    --------
    >>> decoded = links([
    ...     {"Type": "", "Name": "Init", "Node": {"state": {"g": 0}}},
    ...     {"Type": "action", "Name": "Pick", "Node": {"state": {"g": 1}}},
    ...     {"Type": "", "Name": "stutter", "Node": {"state": {"g": 1}}},
    ... ])
    >>> [kind.value for kind in (link.kind for link in decoded)]
    ['initial', 'action', 'stutter']
    >>> decoded[1].name, dict(decoded[1].state)
    ('Pick', {'g': 1})
    """
    if isinstance(entries, str | bytes) or not isinstance(entries, cabc.Sequence):
        msg = f"an error graph is not a list of links: {entries!r}"
        raise TypeError(msg)
    return tuple(
        _link(entry, is_first=index == 0) for index, entry in enumerate(entries)
    )


def _link(entry: object, *, is_first: bool) -> Link:
    """Decode one entry of an error graph.

    Parameters
    ----------
    entry : object
        One entry of the document.
    is_first : bool
        Whether this is the document's first entry, which records the initial
        state rather than a transition.

    Returns
    -------
    Link
        The entry, decoded.

    Raises
    ------
    TypeError
        If the entry is not a mapping.
    ValueError
        If the entry is a mapping but does not name a link.
    """
    if not isinstance(entry, cabc.Mapping):
        msg = f"an error graph link is not a mapping: {entry!r}"
        raise TypeError(msg)
    fields = typ.cast("cabc.Mapping[str, object]", entry)
    state = _state(fields)
    if is_first:
        if fields.get("Type"):
            msg = "the first link is not the initial state"
            raise ValueError(msg)
        return Link(LinkKind.INITIAL, None, state)
    if fields.get("Type") == "action":
        return Link(LinkKind.ACTION, _transition_name(fields, "an action"), state)
    if fields.get("Name") == "stutter":
        return Link(LinkKind.STUTTER, None, state)
    return Link(LinkKind.CHOICE, _transition_name(fields, "a choice"), state)


def _transition_name(fields: cabc.Mapping[str, object], noun: str) -> str:
    """Return the transition label one link records.

    Parameters
    ----------
    fields : collections.abc.Mapping[str, object]
        One decoded entry of the error graph.
    noun : str
        What the link is, for the diagnostic: ``an action`` or ``a choice``.

    Returns
    -------
    str
        The transition's label.

    Raises
    ------
    ValueError
        If the entry carries no label. A label that is not a string is
        refused too: coercing it would invent a step name the checker never
        wrote.
    """
    name = fields.get("Name")
    if not isinstance(name, str):
        msg = f"{noun} link names no transition: {fields!r}"
        raise ValueError(msg)  # ruff: ignore[type-check-without-type-error] - malformed data, not a caller bug.
    return name


def _state(fields: cabc.Mapping[str, object]) -> cabc.Mapping[str, object]:
    """Return the model state one link records.

    Parameters
    ----------
    fields : collections.abc.Mapping[str, object]
        One decoded entry of the error graph.

    Returns
    -------
    collections.abc.Mapping[str, object]
        The state under ``Node.state``.

    Raises
    ------
    ValueError
        If the link carries no ``Node`` mapping, or no ``state`` mapping
        under it. A graph that reached here is shaped wrongly rather than
        merely unfamiliar, and the caller must be able to say so instead of
        leaking a ``KeyError`` from inside the parser.
    """
    node = fields.get("Node")
    if not isinstance(node, cabc.Mapping):
        msg = f"a link carries no Node mapping: {fields!r}"
        raise ValueError(msg)  # ruff: ignore[type-check-without-type-error] - malformed data, not a caller bug.
    state = node.get("state")
    if not isinstance(state, cabc.Mapping):
        msg = f"a link's Node carries no state mapping: {fields!r}"
        raise ValueError(msg)  # ruff: ignore[type-check-without-type-error] - malformed data, not a caller bug.
    return typ.cast("cabc.Mapping[str, object]", state)


def trace(error_graph: str) -> Trace:
    """Return the path a counterexample took.

    Parameters
    ----------
    error_graph : str
        The text of ``error-graph.json``.

    Returns
    -------
    Trace
        The normalized path. A text that is not JSON, or links that are not a
        well-formed path, raise from :func:`links` or
        :func:`modelcheck.trace.normalize_trace` rather than becoming a trace
        this module invented.
    """
    return normalize_trace(links(json.loads(error_graph)))
