"""Type coercion from parsed TOML values to supervisor field types.

Each converter returns ``None`` for a value of the wrong type; no setting is
ever ``None``, so ``None`` cannot be confused with a converted value. The key
type is ``object`` because a ``X | None`` field's annotation is a union object
rather than a ``type``.
"""

import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ["coerce"]


def coerce(annotation: object, value: object) -> object | None:
    """Check and convert one TOML value to its field's type.

    Parameters
    ----------
    annotation : object
        The dataclass field's type object.
    value : object
        The parsed TOML value.

    Returns
    -------
    object | None
        The converted value, or None when the value has the wrong type.

    Examples
    --------
    >>> coerce(int, 3)
    3
    >>> coerce(int, "3") is None
    True
    """
    # Python 3.14 evaluates annotations lazily, so dataclass fields carry the
    # field's type object rather than its source text. A ``X | None`` field
    # carries a ``types.UnionType``, which is not a ``type``, so membership is
    # asked of the table rather than gated on ``isinstance``.
    converter = _CONVERTERS.get(annotation)
    return converter(value) if converter is not None else None


def _is_number(value: object) -> bool:
    """Report whether a TOML value is a number; TOML booleans are not.

    Returns
    -------
    bool
        Whether the value is an integer or a float.
    """
    return isinstance(value, int | float) and not isinstance(value, bool)


_CONVERTERS: dict[object, cabc.Callable[[object], object]] = {
    Path: lambda value: Path(value) if isinstance(value, str) else None,
    Path | None: lambda value: Path(value) if isinstance(value, str) else None,
    int: lambda value: value if _is_number(value) and isinstance(value, int) else None,
    float: lambda value: float(typ.cast("float", value)) if _is_number(value) else None,
    str: lambda value: value if isinstance(value, str) else None,
}
