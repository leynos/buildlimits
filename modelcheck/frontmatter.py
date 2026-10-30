"""Render a FizzBee model's YAML frontmatter from a configuration's options.

A FizzBee variant's bounds are part of what its run means: ``max_actions``,
``max_concurrent_actions``, ``liveness`` and ``deadlock_detection`` each change
which traces exist, so a variant that inherited the fixed model's block would
be exploring a different model than its report claimed. The materializer
therefore renders the whole block from the configuration every time and
inherits nothing.

The block is rendered here rather than dumped by a YAML library, because
``materialize`` is a pure module with a fixed import allowlist. Only the shapes
a FizzBee model uses are supported — mappings, sequences of scalars and
scalars — and anything else raises :class:`MaterializeError` rather than being
guessed at, since a block the checker cannot parse is a syntax error in a file
this tool wrote.

This is a separate module from :mod:`modelcheck.materialize` because rendering
YAML and rewriting model source are different jobs that happened to share a
caller, and together they outgrew the repository's module limit.
"""

import collections.abc as cabc
import json

from modelcheck.domain import MaterializeError


def replace_frontmatter(text: str, options: cabc.Mapping[str, object]) -> str:
    r"""Replace or remove a specification's YAML frontmatter.

    A source whose block is never closed raises :class:`MaterializeError`
    through :func:`body_after_frontmatter`, and an option value that is not a
    scalar or a sequence of scalars raises it through :func:`_render_value`;
    both report a text this tool could not have meant to write.

    Parameters
    ----------
    text : str
        The specification's text.
    options : collections.abc.Mapping[str, object]
        The complete frontmatter the variant runs with. Empty means the
        variant has none, so a bound the fixed model declares is never
        inherited by accident.

    Returns
    -------
    str
        The text with its frontmatter replaced and everything after it
        unchanged.

    Examples
    --------
    >>> replace_frontmatter("---\noptions:\n  max_actions: 4\n---\n\nbody\n", {})
    'body\n'
    """
    body = body_after_frontmatter(text)
    if not options:
        return body
    block = "\n".join(_render_mapping(options, 0))
    return f"---\n{block}\n---\n\n{body}"


def body_after_frontmatter(text: str) -> str:
    r"""Return a specification's text without its frontmatter block.

    Parameters
    ----------
    text : str
        The specification's text.

    Returns
    -------
    str
        Everything after the frontmatter's closing delimiter, with the single
        blank line that follows it removed so that re-rendering a block is
        idempotent; the whole text when it has no frontmatter.

    Raises
    ------
    MaterializeError
        If the text opens a frontmatter block it never closes.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return text
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r\n") == "---":
            rest = lines[index + 1 :]
            if rest and not rest[0].strip():
                rest = rest[1:]
            return "".join(rest)
    msg = "the source's frontmatter is never closed"
    raise MaterializeError(msg)


def _render_mapping(options: cabc.Mapping[str, object], depth: int) -> list[str]:
    """Render one mapping as YAML lines.

    Parameters
    ----------
    options : collections.abc.Mapping[str, object]
        The mapping to render.
    depth : int
        How many levels deep it sits, which sets its indentation.

    Returns
    -------
    list[str]
        One line per entry, nested mappings indented two spaces further.
    """
    pad = "  " * depth
    lines: list[str] = []
    for key, value in options.items():
        if isinstance(value, cabc.Mapping):
            lines.append(f"{pad}{key}:")
            lines.extend(_render_mapping(value, depth + 1))
        else:
            lines.append(f"{pad}{key}: {_render_value(value)}")
    return lines


def _render_value(value: object) -> str:
    """Render one frontmatter value the way YAML spells it.

    Strings are quoted so a value such as ``strict`` cannot be read as a bare
    word, and booleans are spelled the YAML way rather than as Python's.

    Parameters
    ----------
    value : object
        A scalar or a sequence of scalars.

    Returns
    -------
    str
        A quoted string, a bare number, a YAML boolean, or a flow sequence.

    Raises
    ------
    MaterializeError
        If the value is neither a scalar nor a sequence of scalars, since
        rendering it by guesswork would put a block the checker rejects into
        a file this tool wrote.
    """
    match value:
        case bool() | int() | float() | str():
            return json.dumps(value)
        case list() | tuple():
            return f"[{', '.join(_render_value(item) for item in value)}]"
        case _:
            msg = f"the frontmatter value {value!r} is not a scalar or a sequence"
            raise MaterializeError(msg)
