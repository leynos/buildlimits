"""Reconciling a duplicate pair, deleting archives and quarantine moves.

A crash can leave a sealed source beside its archive, and reconciliation
decides which copy is authoritative by verifying both. Expired history is
deleted whole, and anything the run cannot trust is moved into quarantine
under a name that records why and never replaces an earlier entry.
"""

import dataclasses
import lzma
import typing as typ

import pytest
from archive_files_helpers import (
    context,
    document,
    duplicate_pair,
    published,
    quarantined,
    records,
    sealed_entry,
)

from buildlimits.outcome_archive_files import delete_archive
from buildlimits.outcome_archive_fs import UnsafePathError
from buildlimits.outcome_archive_quarantine import (
    quarantine_archive,
    remove_orphan_sidecar,
)
from buildlimits.outcome_archive_reconcile import reconcile_duplicate
from buildlimits.outcome_segments import archive_name, sidecar_name

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_a_matching_duplicate_is_reconciled(tmp_path: Path) -> None:
    """A source whose archive proves the same content is redundant."""
    run, sealed, archived, _data = duplicate_pair(tmp_path)

    result = reconcile_duplicate(run, sealed_entry(sealed, 1), archived)

    assert result.action == "reconciled", "the pair is settled as one archive"
    assert not sealed.exists(), "the redundant source is removed"
    assert archived.path.exists(), "the verified archive stays"
    assert quarantined(tmp_path) == [], "a matching pair needs no quarantine"


def test_a_reconciled_duplicate_records_the_sidecar_it_verified(
    tmp_path: Path,
) -> None:
    """Settling a pair rebuilds a sidecar the archive lost before the source goes.

    Reconciliation skips duplicated sequences in the heal plan, so this is the
    last moment the verified archive's facts can be recorded.
    """
    run, sealed, archived, _data = duplicate_pair(tmp_path, drop_sidecar=True)

    result = reconcile_duplicate(run, sealed_entry(sealed, 1), archived)

    assert result.action == "reconciled", "the pair is settled"
    assert not sealed.exists(), "the verified source is removed"
    assert (run.layout.archive_dir / sidecar_name(1)).exists(), (
        "the archived facts are kept"
    )


def test_a_symlinked_quarantine_directory_is_refused(tmp_path: Path) -> None:
    """A link would move damaged evidence outside the archive tree.

    The target is a private directory, so a move into it would otherwise
    succeed; only validating the quarantine directory makes this test pass.
    """
    run = context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    outside.chmod(0o700)
    run.layout.quarantine_dir.symlink_to(outside, target_is_directory=True)
    damaged = run.layout.archive_dir / archive_name(1)
    damaged.write_bytes(b"junk")
    damaged.chmod(0o600)

    with pytest.raises(UnsafePathError):
        quarantine_archive(run, sealed_entry(damaged, 1), "bad_xz")

    assert not list(outside.iterdir()), "nothing is moved through the link"
    assert damaged.exists(), "the file is still where it was"


def test_a_mismatched_archive_is_replaced_from_its_source(tmp_path: Path) -> None:
    """A stale archive is quarantined and the source archived again."""
    run, sealed, archived, _older = duplicate_pair(tmp_path, count=1)
    source_data = document(records(2, first=1))
    sealed.write_bytes(source_data)
    sealed.chmod(0o600)

    result = reconcile_duplicate(run, sealed_entry(sealed, 1), archived)

    assert result.action == "re-archived", "the source is the authority"
    assert lzma.decompress(archived.path.read_bytes()) == source_data, (
        "the archive now matches the source"
    )
    assert any(".mismatch." in path.name for path in quarantined(tmp_path)), (
        "the stale archive is quarantined as a mismatch"
    )


def test_an_unreadable_archive_is_replaced_from_its_source(tmp_path: Path) -> None:
    """An archive that will not verify is quarantined before re-archiving."""
    run, sealed, archived, data = duplicate_pair(tmp_path, count=1)
    archived.path.write_bytes(b"not xz at all")

    result = reconcile_duplicate(run, sealed_entry(sealed, 1), archived)

    assert result.action == "re-archived", "the source is archived again"
    assert lzma.decompress(archived.path.read_bytes()) == data, "the archive is valid"
    assert ".bad_xz." in quarantined(tmp_path)[0].name, "the fault is recorded"


def test_a_corrupt_source_is_quarantined_and_the_archive_kept(
    tmp_path: Path,
) -> None:
    """A damaged source cannot displace an archive that still verifies."""
    run, sealed, archived, _data = duplicate_pair(tmp_path, count=1)
    sealed.write_bytes(b"not json")
    sealed.chmod(0o600)

    result = reconcile_duplicate(run, sealed_entry(sealed, 1), archived)

    assert result.action == "quarantined", "the damaged source is removed"
    assert result.reason == "corrupt", "its reason is closed"
    assert not sealed.exists(), "the source is quarantined"
    assert archived.path.exists(), "the verified archive is kept"
    assert (run.layout.archive_dir / sidecar_name(1)).exists(), (
        "the kept archive keeps the sidecar that describes it"
    )
    assert ".corrupt." in quarantined(tmp_path)[0].name, "the fault is recorded"


def test_deleting_an_archive_removes_its_sidecar(tmp_path: Path) -> None:
    """Expired history leaves no sidecar that would describe nothing."""
    run, entry = published(tmp_path)

    delete_archive(run, entry)

    assert not entry.path.exists(), "the archive is gone"
    assert not (run.layout.archive_dir / sidecar_name(1)).exists(), (
        "the sidecar is gone"
    )


def test_an_orphan_sidecar_is_quarantined(tmp_path: Path) -> None:
    """A sidecar with no archive is never silently deleted."""
    run = context(tmp_path)
    orphan = run.layout.archive_dir / sidecar_name(2)
    orphan.write_text("{}")

    result = remove_orphan_sidecar(run, 2)

    assert result.action == "quarantined", "the orphan is moved aside"
    assert result.reason == "orphan_sidecar", "its reason is closed"
    assert not orphan.exists(), "nothing describes a missing archive"
    assert ".orphan_sidecar." in quarantined(tmp_path)[0].name, (
        "the quarantine name records why"
    )


def test_quarantine_never_replaces_an_earlier_entry(tmp_path: Path) -> None:
    """Two damaged files sharing a name keep separate quarantine entries."""
    run = context(tmp_path)
    first = run.layout.archive_dir / "first" / archive_name(1)
    second = run.layout.archive_dir / "second" / archive_name(1)
    for path, data in ((first, b"one"), (second, b"two")):
        path.parent.mkdir()
        path.write_bytes(data)
        path.chmod(0o600)

    quarantine_archive(run, sealed_entry(first, 1), "bad_xz")
    quarantine_archive(run, sealed_entry(second, 1), "bad_xz")

    entries = quarantined(tmp_path)
    assert len(entries) == 2, "both files survive"
    assert len({path.name for path in entries}) == 2, (
        "the quarantine names never collide"
    )
    assert sorted(path.read_bytes() for path in entries) == [b"one", b"two"], (
        "each entry keeps the bytes of the file it moved"
    )


def test_a_quarantine_name_carries_the_injected_token(tmp_path: Path) -> None:
    """The quarantine tag comes from the run context, not a hidden random read.

    The tag is what keeps a repeated fault from overwriting its earlier
    evidence, so a test that pins the exact destination proves the move reads
    the context's token rather than drawing one of its own.
    """
    run = dataclasses.replace(
        context(tmp_path), quarantine_token=lambda: "0123456789ab"
    )
    damaged = run.layout.archive_dir / archive_name(1)
    damaged.write_bytes(b"junk")
    damaged.chmod(0o600)

    result = quarantine_archive(run, sealed_entry(damaged, 1), "bad_xz")

    assert result.action == "quarantined", "the archive is moved aside"
    assert [path.name for path in quarantined(tmp_path)] == [
        f"{archive_name(1)}.bad_xz.0123456789ab"
    ], "the destination names the fault and the context's token"


def test_quarantining_an_archive_drops_its_sidecar(tmp_path: Path) -> None:
    """A quarantined archive must not leave a sidecar behind."""
    run = context(tmp_path)
    archive = run.layout.archive_dir / archive_name(1)
    archive.write_bytes(b"junk")
    (run.layout.archive_dir / sidecar_name(1)).write_text("{}")

    result = quarantine_archive(run, sealed_entry(archive, 1), "bad_xz")

    assert result.reason == "bad_xz", "the fault reason is reported"
    assert not (run.layout.archive_dir / sidecar_name(1)).exists(), (
        "the sidecar is gone"
    )
    assert len(quarantined(tmp_path)) == 1, "the archive is moved aside"
