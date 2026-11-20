"""Descriptor-relative directory validation for the outcome archiver.

Directories are walked one component at a time, each opened relative to the
descriptor of the component above it with ``O_NOFOLLOW`` and ``O_DIRECTORY``.
A component is validated before the walk continues, and a missing component is
created through its parent's descriptor, so no step ever re-resolves a path an
attacker could have replaced between validation and use. `outcome_archive_fs`
owns the file primitives that use this walk.
"""

import errno
import os
import stat
import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

_DIRECTORY_MODE = 0o700

__all__ = [
    "UnsafePathError",
    "ensure_private_directory",
    "require_private_chain",
]


class UnsafePathError(OSError):
    """A path is a symlink, a non-directory, or not owned by this user."""


def ensure_private_directory(path: Path) -> None:
    """Create a private directory and refuse a symlinked path.

    Every component is opened relative to the descriptor of the component
    above it, with ``O_NOFOLLOW`` and ``O_DIRECTORY``, and is validated before
    the walk continues. A missing component is created through its parent's
    descriptor and its parent is synced before the walk descends into it, so
    no step ever re-resolves a path an attacker could have replaced between
    validation and use and a created name is durable before anything is
    written beneath it. Existing ancestors keep their mode;
    the components this call creates, and the final directory, are made
    owner-only. A component that is a symlink, not a directory, or shared with
    another account raises `UnsafePathError`; a filesystem failure propagates
    as `OSError`.

    Parameters
    ----------
    path : Path
        The directory to create or verify.
    """
    _walk_chain(path, create=True)


def _walk_chain(path: Path, *, create: bool) -> os.stat_result:
    """Walk one absolute path through directory descriptors.

    Parameters
    ----------
    path : Path
        The directory to walk to.
    create : bool
        Whether missing components may be created.

    Returns
    -------
    os.stat_result
        The metadata of the final component, read through its descriptor.

    Raises
    ------
    UnsafePathError
        If the path is relative, a component is a link or not a directory, or
        a component is shared with another account.
    """
    if not path.is_absolute():
        msg = "an archive path must be absolute"
        raise UnsafePathError(msg)
    parts = path.parts[1:]
    descriptor = os.open(path.anchor, _directory_flags())
    try:
        for index, part in enumerate(parts):
            child, created = _descend(descriptor, part, create=create)
            try:
                _require_private_directory(os.fstat(child), part)
                if created or index == len(parts) - 1:
                    os.fchmod(child, _DIRECTORY_MODE)
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return os.fstat(descriptor)
    finally:
        os.close(descriptor)


def _descend(parent: int, part: str, *, create: bool) -> tuple[int, bool]:
    """Open one path component relative to its parent's descriptor.

    Returns
    -------
    tuple[int, bool]
        The component's descriptor and whether this call created it.

    Raises
    ------
    UnsafePathError
        If the component is a symlink, is not a directory, or is missing when
        ``create`` is false.
    OSError
        If the component cannot be opened or created for another reason.
    """
    try:
        return os.open(part, _directory_flags(), dir_fd=parent), False
    except FileNotFoundError:
        if not create:
            msg = f"archive path component is missing: {part}"
            raise UnsafePathError(msg) from None
    except OSError as error:
        _refuse_non_directory(error, part)
        raise
    os.mkdir(part, _DIRECTORY_MODE, dir_fd=parent)
    try:
        child = os.open(part, _directory_flags(), dir_fd=parent)
    except OSError as error:
        _refuse_non_directory(error, part)
        raise
    try:
        # A new name is durable only once the directory holding it is synced;
        # syncing files inside the child would not preserve the child's own
        # entry. A failure here is reported before anything is written under
        # the new directory, so no published archive can depend on it.
        os.fsync(parent)
    except BaseException:
        os.close(child)
        raise
    return child, True


def _refuse_non_directory(error: OSError, part: str) -> None:
    """Reject a failed open that means the component is a link or not a directory.

    Parameters
    ----------
    error : OSError
        The failure from opening the component.
    part : str
        The component name, used in the error message.

    Raises
    ------
    UnsafePathError
        If the failure is ``ELOOP`` or ``ENOTDIR``.
    """
    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
        msg = f"archive path component is not a directory: {part}"
        raise UnsafePathError(msg) from error


def _directory_flags() -> int:
    """Return the flags that open one directory without following a link.

    Returns
    -------
    int
        Read-only, directory-only, no-follow and close-on-exec flags.
    """
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def require_private_chain(path: Path) -> None:
    """Reject a path whose components are symlinks or shared with others.

    A symlinked or non-directory component could redirect a write outside the
    store. So could a directory another user may rename entries in, which is
    why every component must also be private to this user: after this check an
    attacker cannot swap a checked component for a link before it is used.

    Parameters
    ----------
    path : Path
        An absolute directory path.

    Raises
    ------
    UnsafePathError
        If a component is not a directory reachable without a symlink, is
        writable by another user, is owned by an account other than this one
        or root, or the final component is not owned by this user.
    """
    final = _walk_chain(path, create=False)
    if final.st_uid != os.getuid():
        msg = "an archive directory must be owned by this user"
        raise UnsafePathError(msg)


def _require_private_directory(metadata: os.stat_result, part: str) -> None:
    """Reject a directory another account could rename or replace.

    Ownership matters as much as the write bits: an account that owns a
    directory can rename or replace entries inside it whatever their modes
    say, so only this user and root are trusted. A root-owned directory with
    the sticky bit set is the accepted shared exception, because the sticky
    bit stops another user removing or renaming an entry this account owns.

    Parameters
    ----------
    metadata : os.stat_result
        The result of ``fstat`` on one opened path component.
    part : str
        The component name, used in the error message.

    Raises
    ------
    UnsafePathError
        If the directory belongs to another account, or is writable by its
        group or by every user and is not a trusted root-owned sticky
        directory.
    """
    if metadata.st_uid not in {os.getuid(), 0}:
        msg = f"archive path component is owned by another user: {part}"
        raise UnsafePathError(msg)
    if not metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return
    is_trusted_shared = metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX
    if not is_trusted_shared:
        msg = f"archive path component is writable by another user: {part}"
        raise UnsafePathError(msg)
