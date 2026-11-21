"""Private filesystem primitives the archiver relies on for safety.

These helpers define the trust boundary between the archiver and the rest of
the filesystem: a lock one run holds at a time, directories that no symlink can
redirect, files only the owner can read, and publication that never replaces
something already there. Each test attacks that boundary rather than repeating
the happy path.
"""

import os
from pathlib import Path

import pytest
from segment_test_helpers import mode, swap_after_validation

from buildlimits import outcome_archive_fs
from buildlimits.outcome_archive_dirs import open_private_chain
from buildlimits.outcome_archive_fs import (
    UnsafePathError,
    atomic_write_json,
    ensure_private_directory,
    open_private_regular,
    open_private_temp,
    publish_no_clobber,
    remove_private_files,
    remove_stale_temps,
    require_private_chain,
)


def test_a_private_directory_is_created_and_enforced(tmp_path: Path) -> None:
    """An existing directory with wider access is narrowed."""
    path = tmp_path / "archive"
    path.mkdir(mode=0o755)

    ensure_private_directory(path)

    assert mode(path) == 0o700, "the directory is owner-only after the call"


def test_missing_components_are_created_private(tmp_path: Path) -> None:
    """A nested path is created one owner-only component at a time."""
    path = tmp_path / "one" / "two" / "archive"

    ensure_private_directory(path)

    assert path.is_dir(), "the whole path exists"
    assert {mode(path), mode(path.parent), mode(path.parent.parent)} == {0o700}, (
        "every created component is private, whatever the umask"
    )


def test_a_retry_syncs_the_parent_of_a_component_it_only_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory left by a failed sync is made durable by the next attempt.

    The first attempt creates the directory and fails to sync its parent, so a
    crash could still discard a name the filesystem never committed. Nothing on
    disk records that failure, so the retry must sync the parent of the
    component it merely opens; otherwise the run could publish an archive
    beneath a name that is not durable.
    """
    target = tmp_path / "one" / "archive"
    parent_inode = tmp_path.stat().st_ino
    synced: list[int] = []
    real_fsync = os.fsync
    failed = False

    def fail_at_the_archive_parent(descriptor: int) -> None:
        """Refuse the archive directory's parent once, then record the rest."""
        nonlocal failed
        inode = os.fstat(descriptor).st_ino
        if inode == parent_inode and not failed:
            failed = True
            raise OSError("the parent cannot be synced")
        synced.append(inode)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_at_the_archive_parent)

    with pytest.raises(OSError, match="cannot be synced"):
        ensure_private_directory(target)

    assert (tmp_path / "one").is_dir(), "the created name is left behind"
    assert not target.exists(), "nothing is created beneath the unsynced name"

    synced.clear()
    ensure_private_directory(target)

    assert parent_inode in synced, (
        "the retry syncs the parent of the component it only opened"
    )


def test_a_symlinked_component_is_refused_before_any_mode_change(
    tmp_path: Path,
) -> None:
    """A link is refused before anything is created through it or narrowed.

    The target is a real directory with wider access, so accepting the link
    would both place the new directory under it and change a mode outside the
    archive chain.
    """
    real = tmp_path / "real"
    real.mkdir(mode=0o755)
    real.chmod(0o755)
    (tmp_path / "link").symlink_to(real, target_is_directory=True)

    with pytest.raises(UnsafePathError):
        ensure_private_directory(tmp_path / "link" / "archive")

    assert mode(real) == 0o755, "the directory outside the chain is untouched"
    assert not (real / "archive").exists(), "nothing is created through the link"


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


@pytest.mark.parametrize("shared_mode", [0o770, 0o777])
def test_a_component_writable_by_others_is_refused(
    tmp_path: Path, shared_mode: int
) -> None:
    """Another user could rename an entry inside a shared parent directory.

    The directory exists and is not a symlink, so only the ownership of the
    directory itself distinguishes it from an accepted chain.
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(shared_mode)

    with pytest.raises(UnsafePathError):
        require_private_chain(shared)


def _open_descriptors() -> int:
    """Return how many descriptors this process currently holds.

    Returns
    -------
    int
        The number of entries in ``/proc/self/fd``.
    """
    return len(list(Path("/proc/self/fd").iterdir()))


def test_a_created_chain_leaks_no_descriptor(tmp_path: Path) -> None:
    """Every component descriptor is closed as the walk descends."""
    path = tmp_path / "one" / "two" / "three" / "archive"
    before = _open_descriptors()

    ensure_private_directory(path)

    assert _open_descriptors() <= before, "the walk closes each parent descriptor"


def test_a_refused_chain_leaks_no_descriptor(tmp_path: Path) -> None:
    """A walk that stops at a link still closes every descriptor it opened."""
    real = tmp_path / "real"
    real.mkdir()
    nested = tmp_path / "one" / "two"
    nested.mkdir(parents=True)
    (nested / "link").symlink_to(real, target_is_directory=True)
    before = _open_descriptors()

    with pytest.raises(UnsafePathError):
        ensure_private_directory(nested / "link" / "archive")

    assert _open_descriptors() <= before, "the refused walk closes what it opened"


def test_a_relative_path_is_refused() -> None:
    """A relative path cannot be proven to stay inside the store."""
    with pytest.raises(UnsafePathError):
        require_private_chain(Path("relative/dir"))


def test_a_private_chain_accepts_a_private_directory(tmp_path: Path) -> None:
    """The intended path passes the check unchanged."""
    assert require_private_chain(tmp_path) is None, "an accepted chain returns nothing"


def test_a_read_only_walk_leaves_every_mode_alone(tmp_path: Path) -> None:
    """Validating a chain must not rewrite a mode an operator chose.

    The walk is shared with inspection, dry runs and history reads, so
    narrowing the final directory on a read would make those commands change
    the store. Validation is still applied: only the mode write is skipped.
    """
    path = tmp_path / "archive"
    path.mkdir(mode=0o755)
    path.chmod(0o755)

    assert require_private_chain(path) is None, "the chain is validated"
    assert mode(path) == 0o755, "a read-only walk never narrows a directory"

    descriptor = open_private_chain(path)
    try:
        assert os.fstat(descriptor).st_mode & 0o777 == 0o755, (
            "the descriptor names the directory as it was found"
        )
    finally:
        os.close(descriptor)


def test_a_temporary_file_is_exclusive_and_private(tmp_path: Path) -> None:
    """Two temporaries never share a name and neither is readable by others."""
    first, first_path = open_private_temp(tmp_path, prefix="segment")
    second, second_path = open_private_temp(tmp_path, prefix="segment")
    os.close(first)
    os.close(second)

    assert first_path != second_path, "each temporary has its own name"
    assert first_path.name.startswith(".segment."), "the prefix is applied"
    assert first_path.name.endswith(".tmp"), "the temporary is recognizable"
    assert mode(first_path) == 0o600, "the temporary is private"


def test_a_regular_file_is_opened_without_a_mode_change(tmp_path: Path) -> None:
    """A caller that disables the mode check still receives the metadata."""
    path = tmp_path / "file"
    path.write_bytes(b"data")
    path.chmod(0o644)

    descriptor, metadata = open_private_regular(path, os.O_RDONLY, enforce_mode=False)
    os.close(descriptor)

    assert metadata.st_size == 4, "the metadata describes the file"
    assert mode(path) == 0o644, "the mode is left alone"


def test_a_regular_file_is_narrowed_by_default(tmp_path: Path) -> None:
    """By default the archiver makes what it opens owner-only."""
    path = tmp_path / "file"
    path.write_bytes(b"data")
    path.chmod(0o644)

    descriptor, _metadata = open_private_regular(path, os.O_RDONLY)
    os.close(descriptor)

    assert mode(path) == 0o600, "the mode is enforced"


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
    assert mode(path) == 0o600, "the state is private"
    assert not list(tmp_path.glob(".*.tmp")), "no temporary is left behind"


def test_json_state_replaces_a_previous_document(tmp_path: Path) -> None:
    """A later run overwrites the record from the run before it."""
    path = tmp_path / "last-run.json"
    path.write_text('{"stale":true}\n')

    atomic_write_json(path, {"fresh": True})

    assert path.read_text() == '{"fresh":true}\n', "the old document is gone"


def test_a_non_serializable_document_leaves_no_temporary(tmp_path: Path) -> None:
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


def test_a_removal_follows_the_validated_directory_after_its_path_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deletion uses the pinned descriptor, not a path that may have moved."""
    archive_dir = tmp_path / "archive"
    ensure_private_directory(archive_dir)
    doomed = archive_dir / "segment-000000000001.jsonl.xz"
    doomed.write_bytes(b"real")
    moved = tmp_path / "archive-as-validated"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    decoy = elsewhere / doomed.name
    decoy.write_bytes(b"decoy")
    monkeypatch.setattr(
        outcome_archive_fs,
        "open_private_chain_when_present",
        swap_after_validation(
            outcome_archive_fs.open_private_chain_when_present,
            directory=archive_dir,
            moved=moved,
            elsewhere=elsewhere,
        ),
    )

    remove_private_files(archive_dir, [doomed.name])

    assert not (moved / doomed.name).exists(), "the validated file is removed"
    assert decoy.exists(), "a file beside the replacement is untouched"


def test_removing_from_an_absent_directory_does_nothing(tmp_path: Path) -> None:
    """A directory an earlier run already removed reports no failure."""
    remove_private_files(tmp_path / "absent", ["segment-000000000001.jsonl.xz"])

    assert not list(tmp_path.iterdir()), "nothing is created to remove from"
