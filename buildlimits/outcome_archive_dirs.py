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
    "MissingPathError",
    "UnsafePathError",
    "ensure_private_directory",
    "open_private_chain",
    "open_private_chain_when_present",
    "require_private_chain",
    "require_private_chain_when_present",
]


class UnsafePathError(OSError):
    """A path is a symlink, a non-directory, or not owned by this user."""


class MissingPathError(UnsafePathError):
    """A path component does not exist and this check may not create it."""


def ensure_private_directory(path: Path) -> None:
    """Create a private directory and refuse a symlinked path.

    Every component is opened relative to the descriptor of the component
    above it, with ``O_NOFOLLOW`` and ``O_DIRECTORY``, and is validated before
    the walk continues. Every component is synced into its parent before the
    walk descends into it, whether this call created it or found it, so a
    retry after a failed sync still establishes durability and no step ever
    re-resolves a path an attacker could have replaced between validation and
    use. Existing ancestors keep their mode;
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

    Notes
    -----
    A relative path, a component that is a link or not a directory, or a
    component shared with another account is refused with `UnsafePathError`
    from `_open_chain`. A component missing while ``create`` is false
    propagates `MissingPathError` from `_descend`.
    """
    descriptor = _open_chain(path, create=create)
    try:
        return os.fstat(descriptor)
    finally:
        os.close(descriptor)


def _open_chain(path: Path, *, create: bool) -> int:
    """Walk one absolute path and return its final component's descriptor.

    Parameters
    ----------
    path : Path
        The directory to walk to.
    create : bool
        Whether missing components may be created.

    Returns
    -------
    int
        The final component's open descriptor, which the caller owns and must
        close.

    Raises
    ------
    UnsafePathError
        If the path is relative, a component is a link or not a directory, or
        a component is shared with another account.

    Notes
    -----
    A component missing while ``create`` is false propagates `MissingPathError`
    from `_descend`.
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
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _descend(parent: int, part: str, *, create: bool) -> tuple[int, bool]:
    """Open one path component relative to its parent's descriptor.

    Returns
    -------
    tuple[int, bool]
        The component's descriptor and whether this call created it.

    Raises
    ------
    MissingPathError
        If the component is missing when ``create`` is false.
    OSError
        If the component cannot be opened or created for another reason; a
        link or non-directory is reported as `UnsafePathError`.

    Notes
    -----
    A creation-capable descent syncs the parent even when the component
    already exists. An earlier attempt may have created this name and then
    failed to sync its parent, leaving a directory that a crash could still
    discard, and nothing on disk records that failure. Repeating the sync on
    every attempt is the only way a retry can publish beneath the component
    without trusting state that may never have been committed.
    """
    created = False
    try:
        child = os.open(part, _directory_flags(), dir_fd=parent)
    except FileNotFoundError:
        if not create:
            msg = f"archive path component is missing: {part}"
            raise MissingPathError(msg) from None
        os.mkdir(part, _DIRECTORY_MODE, dir_fd=parent)
        try:
            child = os.open(part, _directory_flags(), dir_fd=parent)
        except OSError as error:
            _refuse_non_directory(error, part)
            raise
        created = True
    except OSError as error:
        _refuse_non_directory(error, part)
        raise
    if not create:
        return child, created
    try:
        # A name is durable only once the directory holding it is synced;
        # syncing files inside the child would not preserve the child's own
        # entry. A failure here is reported before anything is written under
        # the directory, so no published archive can depend on it.
        os.fsync(parent)
    except BaseException:
        os.close(child)
        raise
    return child, created


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


def open_private_chain(path: Path) -> int:
    """Open a validated directory chain and return its final descriptor.

    The descriptor keeps naming the directory that was validated even if the
    path is later replaced by a link or renamed, so a scan, removal or sync
    performed through it cannot be redirected. Validation and use therefore
    share one walk instead of re-resolving the path.

    Parameters
    ----------
    path : Path
        An absolute directory path.

    Returns
    -------
    int
        The final component's open descriptor. The caller owns it and must
        close it.

    Raises
    ------
    UnsafePathError
        If the final component is not owned by this user.
    OSError
        If the final component's metadata cannot be read.

    Notes
    -----
    A relative path, a component that is not a directory reachable without a
    symlink, or a component shared with another account is refused with
    `UnsafePathError` from `_open_chain`. A directory, or parent, that does not
    exist propagates `MissingPathError`.
    """
    descriptor = _open_chain(path, create=False)
    try:
        metadata = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        raise
    if metadata.st_uid != os.getuid():
        os.close(descriptor)
        msg = "an archive directory must be owned by this user"
        raise UnsafePathError(msg)
    return descriptor


def open_private_chain_when_present(path: Path) -> int | None:
    """Open a validated directory chain, or report that it is absent.

    Parameters
    ----------
    path : Path
        An absolute directory path that may be missing.

    Returns
    -------
    int | None
        The final component's open descriptor, which the caller must close,
        or None when a component does not exist.

    Notes
    -----
    A component that exists but is not private to this user is refused with
    `UnsafePathError`, exactly as `open_private_chain` reports it.
    """
    try:
        return open_private_chain(path)
    except MissingPathError:
        # Every component above the missing one was validated before the walk
        # stopped, so an absent directory is the only thing this accepts.
        return None


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

    Notes
    -----
    A component that is not a directory reachable without a symlink, is
    writable by another user or is owned by an account other than this one or
    root is refused with `UnsafePathError` from `open_private_chain`. A
    directory, or parent, that does not exist propagates `MissingPathError`.
    """
    os.close(open_private_chain(path))


def require_private_chain_when_present(path: Path) -> None:
    """Validate a directory chain that may not exist yet.

    The writer creates the sealed directory on its first seal, so a store that
    has never sealed one has no such directory to check. Absence is the only
    tolerated outcome: a directory that exists is validated exactly as
    `require_private_chain` does, so a link or a shared component is still
    refused.

    Parameters
    ----------
    path : Path
        An absolute directory path that may be absent.

    Notes
    -----
    A directory that exists is validated with `require_private_chain`, whose
    refusals propagate unchanged.
    """
    descriptor = open_private_chain_when_present(path)
    if descriptor is not None:
        os.close(descriptor)


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
