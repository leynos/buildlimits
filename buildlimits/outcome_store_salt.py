"""Resolve the outcome-store path and manage its private per-host salt."""

import hashlib
import hmac
import os
import secrets
import tempfile
import typing as typ
from pathlib import Path

from buildlimits.outcome_key import Salt
from buildlimits.outcome_ports import StoreFailure
from buildlimits.outcome_store_io import open_private_regular, write_private_temp

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_SALT_BYTES = 32
_DIRECTORY_MODE = 0o700
_TEMP_SUFFIX = ".tmp"


def store_path(environment: cabc.Mapping[str, str]) -> Path:
    """Resolve the per-user JSON Lines path from an explicit environment.

    Parameters
    ----------
    environment : Mapping[str, str]
        The environment whose XDG state and home directories are used.

    Returns
    -------
    Path
        The managed ``cache-outcomes.jsonl`` path.

    Raises
    ------
    ValueError
        If neither an absolute XDG state directory nor an absolute home exists.

    Examples
    --------
    ``store_path({"HOME": "/home/build"})`` uses the XDG default below home.
    """
    state_home = environment.get("XDG_STATE_HOME")
    if state_home and Path(state_home).is_absolute():
        root = Path(state_home)
    else:
        home = environment.get("HOME")
        if not home or not Path(home).is_absolute():
            msg = "an absolute HOME is required when XDG_STATE_HOME is not absolute"
            raise ValueError(msg)
        root = Path(home) / ".local" / "state"
    return root / "buildlimits" / "cache-outcomes.jsonl"


def file_salt(path: Path) -> Salt | StoreFailure:
    """Read or atomically create one private 32-byte per-host salt.

    Parameters
    ----------
    path : Path
        The salt file path, beside the outcome store.

    Returns
    -------
    Salt or StoreFailure
        Stable key material and its keyed identifier, or ``salt_unavailable``.

    Examples
    --------
    Calling ``file_salt(path)`` twice returns the same salt until the file is
    deliberately removed.
    """
    if not path.is_absolute():
        return StoreFailure("salt_unavailable")
    try:
        _secure_directory(path.parent)
        existing = _read_salt(path)
        if existing is not None:
            return existing
        return _create_salt(path)
    except OSError, ValueError:
        return StoreFailure("salt_unavailable")


def _salt(key: bytes) -> Salt:
    """Build the stable non-secret identifier for one salt key.

    Returns
    -------
    Salt
        Key material with its HMAC-derived identifier.
    """
    salt_id = hmac.new(key, b"buildlimits-salt-id", hashlib.sha256).hexdigest()
    return Salt(key=key, salt_id=salt_id)


def _read_salt(path: Path) -> Salt | None:
    """Read an existing regular salt file, or return None when it is absent.

    Returns
    -------
    Salt or None
        The existing key, or no value when the file does not exist.

    Raises
    ------
    ValueError
        If the key length is invalid.
    """
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor, _ = open_private_regular(path, flags)
    try:
        key = os.read(descriptor, _SALT_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(key) != _SALT_BYTES:
        msg = "salt file has an unsupported length"
        raise ValueError(msg)
    return _salt(key)


def _create_salt(path: Path) -> Salt | StoreFailure:
    """Write a new key through an exclusive temporary file and link.

    Returns
    -------
    Salt or StoreFailure
        The newly created key, the concurrent winner, or a closed failure.

    """
    key = secrets.token_bytes(_SALT_BYTES)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=_TEMP_SUFFIX, dir=path.parent
    )
    temporary = Path(temporary_name)
    write_private_temp(temporary, descriptor, key)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            salt = _read_salt(path)
            if salt is None:
                return StoreFailure("salt_unavailable")
            return salt
        return _salt(key)
    finally:
        temporary.unlink(missing_ok=True)


def _secure_directory(path: Path) -> None:
    """Create the store directory and set its exact private mode."""
    path.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
    path.chmod(_DIRECTORY_MODE)
