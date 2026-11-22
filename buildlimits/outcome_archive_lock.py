"""The archive lock and the disk-headroom guard for one archive run.

The lock is a non-blocking exclusive ``flock`` held for the whole run, so a
second timer firing reports busy instead of interleaving renames. The headroom
guard refuses a run whose temporary files cannot fit, before any segment is
rewritten.
"""

import contextlib
import fcntl
import os
import typing as typ

from buildlimits.outcome_archive_fs import (
    ensure_private_directory,
    require_private_regular,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

_FILE_MODE = 0o600

__all__ = [
    "ArchiveBusyError",
    "ArchiveLock",
    "NoHeadroomError",
    "require_headroom",
]


class ArchiveBusyError(Exception):
    """Another archiver holds the non-blocking lock."""


class NoHeadroomError(OSError):
    """The filesystem cannot hold the temporary files a run needs."""


class ArchiveLock:
    """A non-blocking exclusive lock around one archive directory.

    Parameters
    ----------
    path : Path
        The lock file inside the archive directory.

    Examples
    --------
    ``with ArchiveLock(path):`` raises ``ArchiveBusyError`` instead of waiting
    when a second invocation already holds the lock.
    """

    def __init__(self, path: Path) -> None:
        """Remember the lock file; nothing is opened until entry."""
        self._path = path
        self._descriptor: int | None = None

    def __enter__(self) -> ArchiveLock:
        """Take the lock or report that another run holds it.

        Returns
        -------
        ArchiveLock
            The held lock.

        Raises
        ------
        ArchiveBusyError
            If another process holds the lock.
        OSError
            If the lock file cannot be opened, inspected or locked.
        """
        ensure_private_directory(self._path.parent)
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(self._path, flags, _FILE_MODE)
        try:
            metadata = os.fstat(descriptor)
            require_private_regular(metadata)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise ArchiveBusyError from error
        except OSError:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        return self

    def __exit__(
        self,
        _kind: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: object,
    ) -> None:
        """Release the lock and close its descriptor."""
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def require_headroom(directory: Path, required_bytes: int) -> None:
    """Refuse a run that cannot hold its temporary files.

    Parameters
    ----------
    directory : Path
        The filesystem whose free space must cover ``required_bytes``.
    required_bytes : int
        Temporary bytes the run may need.

    Raises
    ------
    NoHeadroomError
        If the filesystem's available bytes are below the requirement.
    """
    statistics = os.statvfs(directory)
    available = statistics.f_bavail * statistics.f_frsize
    if available < required_bytes:
        msg = "insufficient temporary disk headroom"
        raise NoHeadroomError(msg)
