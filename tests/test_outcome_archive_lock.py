"""The archive lock and the disk-headroom guard.

The lock is a non-blocking exclusive ``flock``: a second timer firing must
report busy rather than wait or interleave renames. The headroom guard refuses
a run whose temporary files cannot fit before any segment is rewritten.
"""

import stat
from pathlib import Path

import pytest

from buildlimits.outcome_archive_lock import (
    ArchiveBusyError,
    ArchiveLock,
    NoHeadroomError,
    require_headroom,
)


def _mode(path: Path) -> int:
    """Return a path's permission bits.

    Returns
    -------
    int
        The ``st_mode`` permission bits without the file type.
    """
    return stat.S_IMODE(path.lstat().st_mode)


def test_a_held_lock_refuses_a_second_run(tmp_path: Path) -> None:
    """A second archiver reports busy instead of waiting for the first."""
    path = tmp_path / "archive" / "archive.lock"

    with ArchiveLock(path), pytest.raises(ArchiveBusyError):
        with ArchiveLock(path):
            pytest.fail("the second lock must not be acquired")


def test_the_lock_is_released_when_left(tmp_path: Path) -> None:
    """Releasing the lock lets the next timer firing take it."""
    path = tmp_path / "archive.lock"

    with ArchiveLock(path):
        pass
    with ArchiveLock(path):
        pass


def test_the_lock_directory_is_private(tmp_path: Path) -> None:
    """Taking the lock creates its directory with owner-only access."""
    directory = tmp_path / "archive"
    lock = directory / "archive.lock"

    with ArchiveLock(lock):
        assert _mode(directory) == 0o700, "the lock directory is private"
        assert _mode(lock) == 0o600, "the lock file is private"


def test_headroom_within_the_free_space_is_accepted(tmp_path: Path) -> None:
    """A requirement the filesystem can meet proceeds."""
    assert require_headroom(tmp_path, 1) is None, "sufficient headroom returns nothing"


def test_headroom_beyond_the_free_space_is_refused(tmp_path: Path) -> None:
    """A run that cannot hold its temporaries reports a retryable failure."""
    with pytest.raises(NoHeadroomError, match="headroom"):
        require_headroom(tmp_path, 1 << 62)
