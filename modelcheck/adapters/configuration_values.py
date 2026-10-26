"""Read the fields a configuration substitutes into the model.

A configuration table declares two kinds of thing: the *values* the model text
is rendered with — constants, fairness levels and frontmatter options, plus the
state floor the run must clear — and the *expectations* the run must prove.
This module owns the first kind; :mod:`modelcheck.adapters.configuration_fields`
owns the second and assembles the configuration from both.

The split is not cosmetic. Values are substituted as source text, so a boolean
must be spelled the model's way, while expectations are names that must match an
assertion the model declares. The two fail differently and are read by
different parts of the materializer, so a reader fixing one should not have to
walk past the other.

Every reader takes ``object`` and checks the shape rather than trusting a cast.
A cast is a statement to the type checker, not a test: a file declaring
``constants = ["CAPACITY"]`` would pass one and then fail somewhere a reader
must trace back to this file. Each check turns that into a message naming the
configuration and the field.
"""

from modelcheck.domain import MaterializeError

__all__ = [
    "fairness",
    "floor",
    "options",
    "strings",
]

_LEVELS = frozenset({"weak", "strong", "unfair"})
"""The fairness levels a configuration may set."""


def strings(name: str, field: str, value: object) -> dict[str, str]:
    """Read a table of scalars as strings.

    Parameters
    ----------
    name : str
        The configuration's name, for the error message.
    field : str
        The field's name, for the error message.
    value : object
        The value the file declared.

    Returns
    -------
    dict[str, str]
        The declared values, as source text.

    Raises
    ------
    MaterializeError
        If the value is not a table, or one of its values is not a scalar that
        can be rendered as source text.
    """
    if not isinstance(value, dict):
        msg = f"configuration {name!r} has a {field!r} that is not a table"
        raise MaterializeError(msg)
    return {key: _text(name, field, item) for key, item in value.items()}


def _text(name: str, field: str, value: object) -> str:
    """Render one declared value as the source text a model substitutes.

    Parameters
    ----------
    name : str
        The configuration's name, for the error message.
    field : str
        The field's name, for the error message.
    value : object
        The value the file declared.

    Returns
    -------
    str
        A string unchanged, a boolean as ``true`` or ``false``, and a number as
        its literal text. The model's own language spells booleans in lower
        case, so a TOML ``true`` substitutes as the model's ``true`` rather
        than as Python's ``True``, which would not compile.

    Raises
    ------
    MaterializeError
        If the value is a table or an array, which no constant substitution can
        express.
    """
    match value:
        case bool():
            return "true" if value else "false"
        case str():
            return value
        case int() | float():
            return str(value)
        case _:
            msg = f"configuration {name!r} has a {field!r} value that is not a scalar"
            raise MaterializeError(msg)


def fairness(name: str, value: object) -> dict[str, str]:
    """Read a configuration's fairness table.

    Parameters
    ----------
    name : str
        The configuration's name, for the error message.
    value : object
        The value the file declared.

    Returns
    -------
    dict[str, str]
        Each tag and the level to give it.

    Raises
    ------
    MaterializeError
        If a level is not ``weak``, ``strong`` or ``unfair``. Checking it here
        rather than in the materializer means the message names the file the
        reader must edit.
    """
    levels = strings(name, "fairness", value)
    for tag, level in levels.items():
        if level not in _LEVELS:
            msg = (
                f"configuration {name!r} gives fairness tag {tag!r} the level "
                f"{level!r}, which is not one of {sorted(_LEVELS)}"
            )
            raise MaterializeError(msg)
    return levels


def options(name: str, value: object) -> dict[str, object]:
    """Read a configuration's frontmatter options.

    Parameters
    ----------
    name : str
        The configuration's name, for the error message.
    value : object
        The value the file declared.

    Returns
    -------
    dict[str, object]
        The options, passed through as the TOML values they were. The
        frontmatter renderer decides how each becomes YAML; this adapter does
        not, so an option the renderer learns to express later needs no change
        here.

    Raises
    ------
    MaterializeError
        If the value is not a table.
    """
    if not isinstance(value, dict):
        msg = f"configuration {name!r} has an 'options' that is not a table"
        raise MaterializeError(msg)
    return dict(value)


def floor(name: str, value: object) -> int:
    """Read a configuration's state floor.

    Parameters
    ----------
    name : str
        The configuration's name, for the error message.
    value : object
        The value the file declared.

    Returns
    -------
    int
        The floor below which a pass proves nothing.

    Raises
    ------
    MaterializeError
        If the value is not a count: a boolean, which Python reads as an
        integer but which is a mistake rather than a floor of one; a negative
        number; or a value that is not a number at all.
    """
    match value:
        case bool():
            raise MaterializeError(_not_a_count(name))
        case int() if value >= 0:
            return value
        case _:
            raise MaterializeError(_not_a_count(name))


def _not_a_count(name: str) -> str:
    """Return the refusal a floor that is not a count earns.

    Parameters
    ----------
    name : str
        The configuration's name.

    Returns
    -------
    str
        The message, naming the configuration to look at.
    """
    return f"configuration {name!r} has a min_states that is not a count"
