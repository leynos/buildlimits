"""Pure validators for JSON-backed boundary values shared by adapters."""

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def json_mapping(value: object) -> cabc.Mapping[str, object] | None:
    """Accept only dictionaries with string keys.

    Returns
    -------
    Mapping[str, object] or None
        The object's fields, or no value for a different JSON shape.

    Examples
    --------
    >>> json_mapping({"stats": {}})["stats"]
    {}
    >>> json_mapping([]) is None
    True
    """
    if not isinstance(value, dict):
        return None
    if not all(isinstance(key, str) for key in value):
        return None
    return typ.cast("cabc.Mapping[str, object]", value)


def nonnegative_integer(value: object) -> int | None:
    """Accept non-negative integers and exclude boolean values.

    Returns
    -------
    int or None
        The count, or no value for a boolean, non-integer or negative value.

    Examples
    --------
    >>> nonnegative_integer(4)
    4
    >>> nonnegative_integer(True) is None
    True
    """
    match value:
        case bool():
            return None
        case int() as integer if integer >= 0:
            return integer
        case _:
            return None
