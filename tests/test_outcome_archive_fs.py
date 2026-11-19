"""Private filesystem primitives the archiver relies on for safety.

These helpers define the trust boundary between the archiver and the rest of
the filesystem: a lock one run holds at a time, directories that no symlink can
redirect, files only the owner can read, and publication that never replaces
something already there. Each test attacks that boundary rather than repeating
the happy path.
"""

import os
import stat
from pathlib import Path

import pytest

from buildlimits.outcome_archive_fs import (
    UnsafePathError,
    atomic_write_json,
    ensure_private_directory,
    open_private_regular,
    open_private_temp,
    publish_no_clobber,
    remove_stale_temps,
    require_private_chain,
)


def _mode(path: Path) -> int:
    """Return a path's permission bits.

    Returns
    -------
    int
        The ``st_mode`` permission bits without the file type.
    """
    return stat.S_IMODE(path.lstat().st_mode)


def test_a_private_directory_is_created_and_enforced(tmp_path: Path) -> None:
    """An existing directory with wider access is narrowed."""
    path = tmp_path / "archive"
    path.mkdir(mode=0o755)

    ensure_private_directory(path)

    assert _mode(path) == 0o700, "the directory is owner-only after the call"


def test_a_symlinked_component_is_refused(tmp_path: Path) -> None:
    """A symlink in the chain could redirect a write outside the store."""
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real, target_is_directory=True)

    with pytest.raises(UnsafePathError):
        require_private_chain(tmp_path / "link")


def test_a_regular_file_component_is_refused(tmp_path: Path) -> None:
    """A file in the middle of the path is not a directory."""
    (tmp_path / "file").write_text("x")

    with pytest.raises(UnsafePathError):
        require_private_chain(tmp_path / "file")


def test_a_missing_component_is_refused(tmp_path: Path) -> None:
    """The chain must exist; this helper never creates a middle directory."""
    with pytest.raises(UnsafePathError):
        require_private_chain(tmp_path / "absent")


def test_a_relative_path_is_refused() -> None:
    """A relative path cannot be proven to stay inside the store."""
    with pytest.raises(UnsafePathError):
        require_private_chain(Path("relative/dir"))


def test_a_private_chain_accepts_a_private_directory(tmp_path: Path) -> None:
    """The intended path passes the check unchanged."""
    assert require_private_chain(tmp_path) is None, "an accepted chain returns nothing"


def test_a_temporary_file_is_exclusive_and_private(tmp_path: Path) -> None:
    """Two temporaries never share a name and neither is readable by others."""
    first, first_path = open_private_temp(tmp_path, prefix="segment")
    second, second_path = open_private_temp(tmp_path, prefix="segment")
    os.close(first)
    os.close(second)

    assert first_path != second_path, "each temporary has its own name"
    assert first_path.name.startswith(".segment."), "the prefix is applied"
    assert first_path.name.endswith(".tmp"), "the temporary is recognisable"
    assert _mode(first_path) == 0o600, "the temporary is private"


def test_a_regular_file_is_opened_without_a_mode_change(tmp_path: Path) -> None:
    """A caller that disables the mode check still receives the metadata."""
    path = tmp_path / "file"
    path.write_bytes(b"data")
    path.chmod(0o644)

    descriptor, metadata = open_private_regular(path, os.O_RDONLY, enforce_mode=False)
    os.close(descriptor)

    assert metadata.st_size == 4, "the metadata describes the file"
    assert _mode(path) == 0o644, "the mode is left alone"


def test_a_regular_file_is_narrowed_by_default(tmp_path: Path) -> None:
    """By default the archiver makes what it opens owner-only."""
    path = tmp_path / "file"
    path.write_bytes(b"data")
    path.chmod(0o644)

    descriptor, _metadata = open_private_regular(path, os.O_RDONLY)
    os.close(descriptor)

    assert _mode(path) == 0o600, "the mode is enforced"


def test_a_symlinked_file_is_refused(tmp_path: Path) -> None:
    """Opening through a link would read or write somewhere else."""
    target = tmp_path / "target"
    target.write_bytes(b"data")
    link = tmp_path / "link"
    link.symlink_to(target)

    with pytest.raises(OSError, match="symbolic links"):
        open_private_regular(link, os.O_RDONLY, enforce_mode=False)


def test_a_directory_is_not_a_regular_file(tmp_path: Path) -> None:
    """Only regular files are accepted, even when the caller asks for one."""
    with pytest.raises(UnsafePathError, match="not a regular file"):
        open_private_regular(tmp_path, os.O_RDONLY, enforce_mode=False)


def test_publication_never_replaces_an_existing_file(tmp_path: Path) -> None:
    """A published archive stays the authority over a crashed retry."""
    destination = tmp_path / "segment.xz"
    destination.write_bytes(b"original")
    descriptor, temporary = open_private_temp(tmp_path, prefix="segment")
    os.close(descriptor)
    Path(temporary).write_bytes(b"replacement")

    with pytest.raises(FileExistsError):
        publish_no_clobber(temporary, destination)

    assert destination.read_bytes() == b"original", "the archive is untouched"
    assert temporary.exists(), "the temporary is kept for inspection"


def test_publication_links_a_new_file(tmp_path: Path) -> None:
    """A fresh destination receives the verified bytes."""
    descriptor, temporary = open_private_temp(tmp_path, prefix="segment")
    os.close(descriptor)
    Path(temporary).write_bytes(b"archive")
    destination = tmp_path / "segment.xz"

    publish_no_clobber(temporary, destination)

    assert destination.read_bytes() == b"archive", "the bytes are published"
    assert temporary.exists(), "the caller still owns the temporary"


def test_json_state_is_written_durably_and_compactly(tmp_path: Path) -> None:
    """State documents are sorted, plain JSON with no leftover temporary."""
    path = tmp_path / "last-run.json"

    atomic_write_json(path, {"b": 2, "a": 1})

    assert path.read_text() == '{"a":1,"b":2}\n', "the document is canonical"
    assert _mode(path) == 0o600, "the state is private"
    assert not list(tmp_path.glob(".*.tmp")), "no temporary is left behind"


def test_json_state_replaces_a_previous_document(tmp_path: Path) -> None:
    """A later run overwrites the record from the run before it."""
    path = tmp_path / "last-run.json"
    path.write_text('{"stale":true}\n')

    atomic_write_json(path, {"fresh": True})

    assert path.read_text() == '{"fresh":true}\n', "the old document is gone"


def test_a_non_serialisable_document_leaves_no_temporary(tmp_path: Path) -> None:
    """A value that cannot be encoded fails before anything is created."""
    path = tmp_path / "last-run.json"

    with pytest.raises(TypeError):
        atomic_write_json(path, {"bad": object()})

    assert not path.exists(), "no state is written"
    assert not list(tmp_path.iterdir()), "no temporary is left behind"


def test_stale_temporaries_are_removed(tmp_path: Path) -> None:
    """A crashed run leaves temporaries that the next run reclaims."""
    (tmp_path / ".segment.0.tmp").write_text("x")
    (tmp_path / ".other.tmp").write_text("x")
    (tmp_path / "kept.json").write_text("x")
    (tmp_path / ".directory.tmp").mkdir()

    removed = remove_stale_temps(tmp_path)

    assert sorted(removed) == [".other.tmp", ".segment.0.tmp"], (
        "only regular temporaries are removed"
    )
    assert (tmp_path / "kept.json").exists(), "real files are kept"
    assert (tmp_path / ".directory.tmp").is_dir(), "directories are never removed"


def test_an_absent_directory_has_no_temporaries(tmp_path: Path) -> None:
    """A store that has never archived has nothing to reclaim."""
    assert not remove_stale_temps(tmp_path / "absent"), (
        "an absent directory has no temporaries to remove"
    )
