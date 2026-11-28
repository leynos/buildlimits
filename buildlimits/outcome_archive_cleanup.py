"""Cleanup of abandoned temporary files beside the outcome store.

The archiver writes temporaries into the archive directory and, for an
in-place sealed rewrite, into the directory the writer also uses for its own
seals. Cleanup therefore walks one validated directory descriptor and removes
only the names a caller's predicate claims, so a concurrent writer's file is
never touched.
"""

import os
import stat
import typing as typ

from buildlimits.outcome_archive_dirs import open_private_chain_when_present

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

TEMP_SUFFIX = ".tmp"
"""The suffix every archive or maintenance temporary carries."""

__all__ = ["TEMP_SUFFIX", "remove_maintenance_files", "remove_stale_temps"]


def remove_stale_temps(directory: Path) -> tuple[str, ...]:
    """Remove abandoned private temporary files under the archive lock.

    Returns
    -------
    tuple[str, ...]
        Names that were removed.
    """
    return _remove_matching(
        directory,
        lambda name: name.endswith(TEMP_SUFFIX),
        tolerate_failures=True,
    )


def _remove_matching(
    directory: Path,
    matches: cabc.Callable[[str], bool],
    *,
    tolerate_failures: bool = False,
) -> tuple[str, ...]:
    """Remove the regular files one predicate names, through one descriptor.

    Parameters
    ----------
    directory : Path
        The directory to clean.
    matches : Callable[[str], bool]
        Whether one file name is abandoned work.
    tolerate_failures : bool
        Whether a file that cannot be inspected or removed is skipped, as a
        best-effort cleanup allows.

    Returns
    -------
    tuple[str, ...]
        Names that were removed; a failure propagates when not tolerated.
    """
    descriptor = open_private_chain_when_present(directory)
    if descriptor is None:
        return ()
    try:
        with os.scandir(descriptor) as iterator:
            removed = [
                entry.name
                for entry in iterator
                if matches(entry.name)
                and _remove_entry(
                    descriptor, entry, tolerate_failures=tolerate_failures
                )
            ]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return tuple(removed)


def _remove_entry(
    descriptor: int,
    entry: os.DirEntry[str],
    *,
    tolerate_failures: bool,
) -> bool:
    """Remove one listed entry, reporting whether it is now gone.

    Returns
    -------
    bool
        True when this call removed the entry; a failure propagates unless
        ``tolerate_failures`` allows it.
    """
    if not _is_regular_listing(entry, tolerate_failures=tolerate_failures):
        return False
    return _unlink_entry(descriptor, entry, tolerate_failures=tolerate_failures)


def _is_regular_listing(entry: os.DirEntry[str], *, tolerate_failures: bool) -> bool:
    """Whether one directory entry is a regular file this pass may remove.

    Returns
    -------
    bool
        True when the entry is a regular file.

    Raises
    ------
    OSError
        If the entry cannot be inspected and ``tolerate_failures`` is false.
    """
    try:
        metadata = entry.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        if not tolerate_failures:
            raise
        return False
    return stat.S_ISREG(metadata.st_mode)


def _unlink_entry(
    descriptor: int, entry: os.DirEntry[str], *, tolerate_failures: bool
) -> bool:
    """Remove one listed entry, reporting whether this call removed it.

    Returns
    -------
    bool
        True when the entry was removed.

    Raises
    ------
    OSError
        If the entry cannot be removed and ``tolerate_failures`` is false.
    """
    try:
        os.unlink(entry.name, dir_fd=descriptor)
    except FileNotFoundError:
        return False
    except OSError:
        if not tolerate_failures:
            raise
        return False
    return True


def remove_maintenance_files(directory: Path, prefix: str) -> tuple[str, ...]:
    """Remove abandoned maintenance temporaries under the archive lock.

    The sealed directory is shared with the writer, which publishes metadata
    through its own temporary files while a seal is in progress. Only names
    carrying the maintenance prefix are removed, so a concurrent seal can never
    lose the file it is about to install.

    Parameters
    ----------
    directory : Path
        The directory whose abandoned maintenance files are removed.
    prefix : str
        The maintenance prefix, matched as ``.<prefix>-``.

    Returns
    -------
    tuple[str, ...]
        Names that were removed.

    Notes
    -----
    A missing directory holds nothing. A refused chain or a failed removal or
    sync propagates as `OSError`.
    """
    marker = f".{prefix}-"
    return _remove_matching(
        directory,
        lambda name: name.startswith(marker) and name.endswith(TEMP_SUFFIX),
    )
