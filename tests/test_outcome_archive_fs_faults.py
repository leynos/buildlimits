"""Filesystem failures the archive primitives translate or clean up after.

Each primitive owns one failure boundary: a temporary file that cannot be made
private is removed, a publication whose directory sync fails is withdrawn, a
state write leaves no abandoned temporary, and an entry the walk cannot
validate is reported as unsupported rather than trusted. These tests force
each failure at the primitive, so the cleanup contract is pinned separately
from the operations that call it.
"""

import collections.abc as cabc
import contextlib
import errno
import os
from pathlib import Path
from unittest import mock

import pytest
from outcome_archive_helpers import refuse

from buildlimits import (
    outcome_archive_dirs,
    outcome_archive_fs,
)
from buildlimits.outcome_archive_cleanup import remove_stale_temps
from buildlimits.outcome_archive_fs import (
    UnsafePathError,
    atomic_write_json,
    open_private_temp,
    publish_no_clobber,
    require_private_regular,
)


def test_a_temporary_that_cannot_be_made_private_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused temporary leaves no file behind for the next scan to find."""

    def refuse_unsafe(*_args: object, **_kwargs: object) -> None:
        """Refuse the private-file validation."""
        raise UnsafePathError("archive path is not a regular file", errno.EINVAL)

    monkeypatch.setattr(outcome_archive_fs, "require_private_regular", refuse_unsafe)

    with pytest.raises(UnsafePathError):
        open_private_temp(tmp_path, prefix="state")

    assert not list(tmp_path.iterdir()), "the refused temporary is unlinked"


def test_a_publication_whose_sync_fails_is_withdrawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A destination that was never made durable is not left looking published."""
    temporary = tmp_path / "state.tmp"
    temporary.write_text("{}")
    destination = tmp_path / "state.json"
    monkeypatch.setattr(outcome_archive_fs, "fsync_directory", refuse)

    with pytest.raises(OSError, match="injected failure"):
        publish_no_clobber(temporary, destination)

    assert not destination.exists(), "the unpublished destination is removed"
    assert temporary.exists(), "the caller still owns the verified temporary"


def test_a_state_write_that_fails_leaves_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed write is not retried against a half-written temporary."""
    monkeypatch.setattr(outcome_archive_fs, "write_all", refuse)

    with pytest.raises(OSError, match="injected failure"):
        atomic_write_json(tmp_path / "retention.json", {"schema": 1})

    assert not list(tmp_path.iterdir()), "nothing is left for the next run"


def test_a_state_sync_that_fails_keeps_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sync failure reports the fault and abandons the temporary name."""
    path = tmp_path / "retention.json"
    monkeypatch.setattr(outcome_archive_fs, "fsync_directory", refuse)

    with pytest.raises(OSError, match="injected failure"):
        atomic_write_json(path, {"schema": 1})

    assert not list(tmp_path.glob("*.tmp")), "the temporary name is released"
    assert path.exists(), "the replaced document is still readable"


def test_a_stale_temporary_that_cannot_be_removed_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry the run cannot remove is left for the next holding of the lock."""
    stale = tmp_path / ".state.abcdef.tmp"
    stale.write_text("{}")

    refuse = mock.Mock(side_effect=OSError(errno.EBUSY, "the entry is held"))
    monkeypatch.setattr(outcome_archive_fs.os, "unlink", refuse)

    removed = remove_stale_temps(tmp_path)

    assert not removed, "a removal that failed is not reported as removed"
    assert stale.exists(), "the held entry survives"
    # The removal is attempted by scanned name only, never by a re-resolved
    # path that another account could have replaced since the scan.
    assert [call.args for call in refuse.call_args_list] == [(stale.name,)], (
        "the held entry is addressed by the name the scan returned"
    )


class _UnreadableEntry:
    """A directory entry whose metadata cannot be read."""

    name = ".state.abcdef.tmp"
    path = "/unreadable/.state.abcdef.tmp"

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        """Fail as an entry removed between the scan and the stat would."""
        raise OSError(errno.ENOENT, "the entry vanished")


class _Entries:
    """A scan result holding one entry whose metadata fails."""

    def __iter__(self) -> cabc.Iterator[_UnreadableEntry]:
        """Return the failing entry once."""
        return iter([_UnreadableEntry()])

    def close(self) -> None:
        """Release the scan, which holds no descriptor."""


def test_an_entry_whose_metadata_cannot_be_read_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vanished entry is neither removed nor reported."""
    monkeypatch.setattr(outcome_archive_fs.os, "scandir", lambda _directory: _Entries())

    removed = remove_stale_temps(Path("/unreadable"))

    assert not removed, "an unreadable entry removes nothing"


def test_a_file_owned_by_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ownership is part of being private, not just the mode bits."""
    path = tmp_path / "state"
    path.write_text("{}")
    metadata = path.stat()
    monkeypatch.setattr(outcome_archive_fs.os, "getuid", lambda: metadata.st_uid + 1)

    with pytest.raises(UnsafePathError, match="owned by this user"):
        require_private_regular(metadata)


def test_a_component_owned_by_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory another account owns could be replaced under the walk."""
    directory = tmp_path / "store"
    outcome_archive_dirs.ensure_private_directory(directory)
    metadata = directory.stat()
    monkeypatch.setattr(outcome_archive_dirs.os, "getuid", lambda: metadata.st_uid + 1)

    with pytest.raises(UnsafePathError, match="owned by another user"):
        outcome_archive_dirs.require_private_chain(directory)


def test_a_final_directory_owned_by_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final component must belong to this user even when the walk passes."""
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    real_fstat = os.fstat
    metadata = real_fstat(descriptor)
    foreign = os.stat_result((
        metadata.st_mode,
        0,
        0,
        0,
        metadata.st_uid + 1,
        0,
        0,
        0,
        0,
        0,
    ))

    def _foreign_final(candidate: int) -> os.stat_result:
        return foreign if candidate == descriptor else real_fstat(candidate)

    monkeypatch.setattr(
        outcome_archive_dirs, "_open_chain", lambda *_args, **_kwargs: descriptor
    )
    monkeypatch.setattr(outcome_archive_dirs.os, "fstat", _foreign_final)
    try:
        with pytest.raises(UnsafePathError, match="owned by this user"):
            outcome_archive_dirs.require_private_chain(tmp_path)
    finally:
        # The refusal closes the descriptor itself; closing again is only
        # insurance for a test that fails before reaching the check.
        with contextlib.suppress(OSError):
            os.close(descriptor)


def test_a_component_error_other_than_a_link_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permission failure is a filesystem fault, not a safety verdict."""
    real_open = os.open

    def refuse_component(
        target: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        """Fail every component open after the anchor."""
        if dir_fd is not None:
            raise OSError(errno.EACCES, "permission denied")
        return real_open(target, flags, mode=mode)

    monkeypatch.setattr(outcome_archive_dirs.os, "open", refuse_component)

    with pytest.raises(OSError, match="permission denied") as raised:
        outcome_archive_dirs.require_private_chain(tmp_path)

    assert raised.value.errno == errno.EACCES, "the original failure is reported"
