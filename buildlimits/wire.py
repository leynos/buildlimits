"""Checked conversions from decoded JSON values to protocol field types.

Each helper either returns the value with a precise type or raises
:class:`~buildlimits.framing.ProtocolError`; none coerces.
"""

from buildlimits.framing import ProtocolError


def expect[T](value: object, kind: type[T], description: str) -> T:
    """Require a decoded JSON value of one Python type.

    Parameters
    ----------
    value : object
        The decoded value.
    kind : type[T]
        The type it must have. ``bool`` is never accepted as ``int``.
    description : str
        The JSON type's name, for the error message.

    Returns
    -------
    T
        The value, narrowed to ``kind``.

    Raises
    ------
    ProtocolError
        If the value has another type.
    """
    confusable = kind is int and isinstance(value, bool)
    if confusable or not isinstance(value, kind):
        msg = f"expected {description}"
        raise ProtocolError(msg)
    return value


def as_list(value: object) -> list[object]:
    """Require a JSON array; see :func:`expect`.

    Returns
    -------
    list[object]
        The array.
    """
    return expect(value, list, "an array")


def as_dict(value: object) -> dict[str, object]:
    """Require a JSON object; see :func:`expect`.

    Returns
    -------
    dict[str, object]
        The object.
    """
    return expect(value, dict, "an object")


def as_str(value: object) -> str:
    """Require a JSON string; see :func:`expect`.

    Returns
    -------
    str
        The string.
    """
    return expect(value, str, "a string")


def as_int(value: object) -> int:
    """Require a JSON integer, not a boolean; see :func:`expect`.

    Returns
    -------
    int
        The integer.
    """
    return expect(value, int, "an integer")


def as_bool(value: object) -> bool:
    """Require a JSON boolean; see :func:`expect`.

    Returns
    -------
    bool
        The boolean.
    """
    return expect(value, bool, "a boolean")


def as_optional_int(value: object) -> int | None:
    """Require a JSON integer or null.

    Returns
    -------
    int | None
        The integer, or ``None`` for null.
    """
    return None if value is None else as_int(value)


def as_str_tuple(value: object) -> tuple[str, ...]:
    """Require an array of strings.

    Returns
    -------
    tuple[str, ...]
        The strings.
    """
    return tuple(as_str(item) for item in as_list(value))


def as_str_dict(value: object) -> dict[str, str]:
    """Require an object whose values are strings.

    Returns
    -------
    dict[str, str]
        The object.
    """
    return {as_str(key): as_str(item) for key, item in as_dict(value).items()}


def as_limit_pair(value: object) -> tuple[int, int]:
    """Require a ``(soft, hard)`` pair of integers.

    Returns
    -------
    tuple[int, int]
        The pair.

    Raises
    ------
    ProtocolError
        If the array does not hold exactly two integers.
    """
    match as_list(value):
        case [soft, hard]:
            return as_int(soft), as_int(hard)
        case _:
            msg = "a resource limit is a (soft, hard) pair"
            raise ProtocolError(msg)
