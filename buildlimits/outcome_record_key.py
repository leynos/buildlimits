"""Bounded validation of a persisted cache key and its toolchain identity.

A cache key is the one part of a store row that names a compilation, so the
format refuses any value that could carry a local path or a malformed digest.
These checks live apart from the record shape they protect: they are about the
key alone, and both the encoder and decoder apply them identically.
"""

import typing as typ

if typ.TYPE_CHECKING:
    from buildlimits.outcome_key import CacheKey

__all__ = [
    "_DIGEST_HEX_LENGTH",
    "_is_choice",
    "_optional_choice",
    "_optional_digest",
    "_path_like",
    "_safe_component",
    "_safe_origin",
    "_safe_target",
    "_text",
    "_valid_digest",
    "_validate_key",
]

_DIGEST_HEX_LENGTH = 64


_INVALID_KEY = "cache key fields are invalid"
"""The refusal an unencodable key raises, so a caller can recognize it."""


def _validate_key(key: CacheKey) -> None:
    """Reject cache keys containing local paths or invalid digests.

    Raises
    ------
    ValueError
        If a key field is empty, malformed or path-bearing. The message is a
        fixed sentence rather than a description of the offending value: a
        refusal reaches an operator's terminal and a log, and the value it
        refused may be exactly the local path this check exists to keep out.
    """
    identity = key.toolchain_identity
    is_valid = all((
        _safe_component(key.crate_name),
        _safe_origin(key.crate_origin),
        _safe_target(key.target),
        _valid_digest(key.argument_digest) is not None,
        _valid_digest(identity.compiler_digest) is not None,
        _valid_digest(identity.rustup_toolchain_digest) is not None,
        _text(identity.cache_version) is not None,
        _text(identity.server_version) is not None,
    ))
    if not is_valid:
        raise ValueError(_INVALID_KEY)


def _safe_component(value: object) -> bool:
    """Check a crate-name component without filesystem separators.

    Returns
    -------
    bool
        Whether the value is a non-empty, path-free string.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    return not any(separator in value for separator in ("/", "\\"))


def _safe_origin(value: object) -> bool:
    """Check a classified origin label without retaining path components.

    Returns
    -------
    bool
        Whether the optional origin is a supported path-free label.
    """
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    if not value.startswith(("registry:", "git:", "path:")):
        return False
    return not any(separator in value for separator in ("/", "\\"))


def _safe_target(value: object) -> bool:
    """Check a target triple or keyed target-path digest.

    Returns
    -------
    bool
        Whether the target is safe to persist.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    if value.startswith("path:"):
        return _valid_digest(value.removeprefix("path:")) is not None
    return not _path_like(value)


def _path_like(value: str) -> bool:
    """Identify Unix, relative and Windows path spellings.

    Returns
    -------
    bool
        Whether the value resembles a local filesystem path.
    """
    return any((
        any(separator in value for separator in ("/", "\\")),
        value.startswith((".", "~")),
        len(value) > 1 and value[1] == ":",
    ))


def _text(value: object) -> str | None:
    """Accept a non-empty string value.

    Returns
    -------
    str or None
        The original string, or no value for empty or non-string input.
    """
    return value if isinstance(value, str) and value.strip() else None


def _is_choice(value: object, allowed: frozenset[str]) -> bool:
    """Check a string against a closed vocabulary.

    Returns
    -------
    bool
        Whether the string belongs to the allowed values.
    """
    return isinstance(value, str) and value in allowed


def _optional_choice(value: object, allowed: frozenset[str]) -> bool:
    """Check an absent value or a string from a closed vocabulary.

    Returns
    -------
    bool
        Whether the value is None or belongs to the allowed values.
    """
    return value is None or _is_choice(value, allowed)


def _valid_digest(value: object) -> str | None:
    """Accept lowercase SHA-256 hexadecimal digests.

    Returns
    -------
    str or None
        The digest, or no value for a malformed representation.
    """
    if not isinstance(value, str) or len(value) != _DIGEST_HEX_LENGTH:
        return None
    if not all(character in "0123456789abcdef" for character in value):
        return None
    return value


def _optional_digest(value: object) -> bool:
    """Check an absent generation digest or a well-formed one.

    A row that names no generation is valid: an older row, a changed window and
    an unreadable server identity all leave the field absent. A digest that is
    present must be the same shape as every other salted digest the store
    holds, so a truncated or hand-edited value is refused rather than counted
    as a generation of its own.

    Returns
    -------
    bool
        Whether the value is None or a lowercase SHA-256 hexadecimal digest.
    """
    return value is None or _valid_digest(value) is not None
