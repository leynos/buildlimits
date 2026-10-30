"""Build one configuration from the table its file declares.

:class:`~modelcheck.adapters.configuration.TomlConfigurations` owns the *file
layout*: which files a model directory holds, where the configurations live,
and what an unreadable file means. This module owns the *table shapes*: what a
``holds`` list may contain, how a counterexample kind is named, and how the
declared fields are assembled into a configuration.
:mod:`modelcheck.adapters.configuration_values` owns the value fields the model
text is rendered with. Keeping the three apart lets a reader looking for the
schema find it without reading the file handling, and keeps each module within
the house line limit.

Every reader here takes ``object`` and checks the shape rather than trusting a
cast. A cast is a statement to the type checker, not a test: a file declaring
``holds = "S1"`` would pass one and then iterate the string one character at a
time, producing two expectations called ``S`` and ``1``. Each check turns that
into a message naming the configuration, because a bare TOML error would not
say which variant the reader must open.
"""

import typing as typ

from modelcheck.adapters.configuration_values import fairness, floor, options, strings
from modelcheck.domain import (
    Configuration,
    CounterexampleKind,
    FailsAs,
    Holds,
    MaterializeError,
    Reachable,
)

if typ.TYPE_CHECKING:
    from modelcheck.domain import Expectation

__all__ = ["configuration"]

_KINDS = {kind.value: kind for kind in CounterexampleKind}
"""The kinds a ``fails_as`` table may name, by their lower-case spelling."""

_SHAPES = {list: "list", dict: "table"}
"""What each container is called in a reader's error message.

The words are TOML's, because the message is read beside the file that got it
wrong: ``holds = "S1"`` should be told it needs a list, and ``fails_as = []``
that it needs a table.
"""


def configuration(name: str, table: dict[str, object]) -> Configuration:
    """Build one configuration from its table.

    Parameters
    ----------
    name : str
        The configuration's name, which is its table's key.
    table : dict[str, object]
        The table's fields.

    Returns
    -------
    Configuration
        The configuration the table describes.

    Raises
    ------
    MaterializeError
        If a field has the wrong shape or names a value the domain does not
        know. The field readers in this module and in
        :mod:`modelcheck.adapters.configuration_values` raise it, naming the
        configuration and the field that is wrong.
    """  # ruff: ignore[docstring-extraneous-exception] - the field readers raise it.
    return Configuration(
        name=name,
        constants=strings(name, "constants", table.get("constants", {})),
        fairness=fairness(name, table.get("fairness", {})),
        options=options(name, table.get("options", {})),
        min_states=floor(name, table.get("min_states", 0)),
        expectations=_expectations(
            name,
            table.get("holds", []),
            table.get("reachable", []),
            table.get("fails_as", {}),
        ),
    )


def _expectations(
    name: str,
    holds: object,
    reachable: object,
    fails_as: object,
) -> tuple[Expectation, ...]:
    """Build a configuration's expectations, in a stable order.

    Parameters
    ----------
    name : str
        The configuration's name, for the error message.
    holds : object
        The properties expected to hold, as the file declared them.
    reachable : object
        The ``exists`` probes expected to be witnessed, as declared.
    fails_as : object
        Each property expected to fail and the kind of counterexample it must
        produce, as declared.

    Returns
    -------
    tuple[Expectation, ...]
        The holds, then the probes, then the expected failures. Holds first
        because that is the order a reader of the model thinks in: what must
        keep holding, then what must be reachable, then what must still be
        broken.

    Raises
    ------
    MaterializeError
        If a field is not the shape it must be — a list of names for ``holds``
        and ``reachable``, a table of names for ``fails_as`` — or if a name is
        not a string or a kind is not one the domain declares. The shape
        readers and ``_word`` and ``_kind`` raise it.

    Notes
    -----
    The fields arrive as ``object`` and are checked here rather than cast at
    the call site. Checking the shape is what turns a mistake such as
    ``holds = "S1"`` into a message naming the configuration.
    """  # ruff: ignore[docstring-extraneous-exception] - the shape readers raise it.
    declared_holds = _shape(name, "holds", holds, list)
    declared_reachable = _shape(name, "reachable", reachable, list)
    declared_failures = _shape(name, "fails_as", fails_as, dict)
    return (
        *(Holds(_word(name, "holds", item)) for item in declared_holds),
        *(Reachable(_word(name, "reachable", item)) for item in declared_reachable),
        *(
            FailsAs(property_name, _kind(name, property_name, kind))
            for property_name, kind in declared_failures.items()
        ),
    )


def _shape[Shape: (list, dict)](
    name: str, field: str, value: object, shape: type[Shape]
) -> Shape:
    """Read a field, refusing a value that is not the container it must be.

    Parameters
    ----------
    name : str
        The configuration's name, for the error message.
    field : str
        The field's name, for the error message.
    value : object
        The value the file declared.
    shape : type[Shape]
        The container the field must be, ``list`` or ``dict``.

    Returns
    -------
    Shape
        The declared value, narrowed to the container the caller asked for.

    Raises
    ------
    MaterializeError
        If the value is not the container the caller asked for. A string is
        refused even though it is a sequence, because iterating it would
        produce one expectation per character.

    Examples
    --------
    >>> _shape("head", "holds", ["S1"], list)
    ['S1']
    >>> _shape("head", "fails_as", {"L3": "lasso"}, dict)
    {'L3': 'lasso'}
    """
    if not isinstance(value, shape):
        msg = (
            f"configuration {name!r} has a {field!r} that is not "
            f"a {_SHAPES.get(shape, shape.__name__)}"
        )
        raise MaterializeError(msg)
    return typ.cast("Shape", value)


def _word(name: str, field: str, value: object) -> str:
    """Read one property or probe name.

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
        The name.

    Raises
    ------
    MaterializeError
        If the value is not a string.
    """
    if not isinstance(value, str):
        msg = f"configuration {name!r} has a {field!r} entry that is not a name"
        raise MaterializeError(msg)
    return value


def _kind(name: str, property_name: str, kind: object) -> CounterexampleKind:
    """Read the counterexample kind an expected failure must produce.

    Parameters
    ----------
    name : str
        The configuration's name, for the error message.
    property_name : str
        The property the failure is about, for the error message.
    kind : object
        The value the file declared.

    Returns
    -------
    CounterexampleKind
        The kind.

    Raises
    ------
    MaterializeError
        If the kind is not one of the domain's, so a typo in the file cannot
        silently expect a kind no run can produce.
    """
    match kind:
        case str() if kind in _KINDS:
            return _KINDS[kind]
        case _:
            msg = (
                f"configuration {name!r} expects {property_name!r} to fail with "
                f"{kind!r}, which is not one of {sorted(_KINDS)}"
            )
            raise MaterializeError(msg)
