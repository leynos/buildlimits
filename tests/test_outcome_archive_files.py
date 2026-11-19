"""Per-segment archive operations and their crash-safe outcomes.

Each operation here runs under the archive lock and may publish, rewrite,
quarantine or delete a file. The tests drive the real filesystem so a failure
is observed the way the next timer firing would see it: the source still
readable, a stale archive quarantined rather than trusted, and a published
sidecar describing exactly the bytes that were verified.
"""

import datetime as dt
import lzma
from pathlib import Path

import pytest
from outcome_test_helpers import attributed_record

from buildlimits import outcome_record_encode
from buildlimits.outcome_archive_context import ArchiveContext
from buildlimits.outcome_archive_encode import read_archive_facts, read_source_facts
from buildlimits.outcome_archive_files import (
    archive_source,
    delete_archive,
    recompress_archive,
    reconcile_duplicate,
    repair_sidecar,
)
from buildlimits.outcome_archive_fs import ensure_private_directory
from buildlimits.outcome_archive_quarantine import (
    quarantine_archive,
    remove_orphan_sidecar,
)
from buildlimits.outcome_archive_verify import SegmentFaultError, Trim
from buildlimits.outcome_record_format import StoreHeader, StoreRecord
from buildlimits.outcome_retention import RetentionPolicy
from buildlimits.outcome_segment_inventory import SegmentEntry, read_inventory
from buildlimits.outcome_segments import (
    SegmentLayout,
    archive_name,
    sealed_meta_name,
    segment_name,
    sidecar_name,
)

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)


def _records(count: int, *, first: int = 0) -> tuple[StoreRecord, ...]:
    """Build ``count`` records observed one minute apart.

    Returns
    -------
    tuple[StoreRecord, ...]
        The records, oldest first.
    """
    return tuple(
        attributed_record(observed_at=NOW - dt.timedelta(minutes=index))
        for index in range(first + count - 1, first - 1, -1)
    )


def _document(records: tuple[StoreRecord, ...], *, salt_id: str = "salt-1") -> bytes:
    """Encode a header and records as one JSON Lines document.

    Returns
    -------
    bytes
        The complete, newline-terminated document.
    """
    lines = [outcome_record_encode.encode_line(StoreHeader(salt_id, NOW))]
    lines.extend(outcome_record_encode.encode_line(record) for record in records)
    return ("\n".join(lines) + "\n").encode()


def _source(tmp_path: Path, sequence: int, data: bytes) -> Path:
    """Write one private sealed source and its writer metadata.

    Returns
    -------
    Path
        The source path.
    """
    layout = SegmentLayout.beside(tmp_path / "cache-outcomes.jsonl")
    layout.sealed_dir.mkdir(parents=True, exist_ok=True)
    path = layout.sealed_dir / segment_name(sequence)
    path.write_bytes(data)
    path.chmod(0o600)
    (layout.sealed_dir / sealed_meta_name(sequence)).write_text("{}")
    return path


def _layout(tmp_path: Path) -> SegmentLayout:
    """Return the layout for a store below ``tmp_path``.

    Returns
    -------
    SegmentLayout
        The layout shared by the tests.
    """
    return SegmentLayout.beside(tmp_path / "cache-outcomes.jsonl")


def _context(
    tmp_path: Path,
    *,
    preset: int = 0,
    policy: RetentionPolicy | None = None,
) -> ArchiveContext:
    """Build one run context, creating the private archive directory.

    Returns
    -------
    ArchiveContext
        The context the operation needs.
    """
    layout = _layout(tmp_path)
    ensure_private_directory(layout.archive_dir)
    return ArchiveContext(
        layout=layout,
        policy=RetentionPolicy() if policy is None else policy,
        preset=preset,
        now=NOW,
    )


def _sealed_entry(path: Path, sequence: int) -> SegmentEntry:
    """Describe one sealed source as the inventory would.

    Returns
    -------
    SegmentEntry
        The source entry.
    """
    return SegmentEntry(
        sequence=sequence,
        path=path,
        state="sealed",
        physical_bytes=path.stat().st_size,
        logical_bytes=path.stat().st_size,
    )


def _archived_entry(tmp_path: Path, sequence: int) -> SegmentEntry:
    """Return the inventory's view of one published archive.

    Returns
    -------
    SegmentEntry
        The archived entry.
    """
    return read_inventory(_layout(tmp_path)).archived[0]


def _quarantined(tmp_path: Path) -> list[Path]:
    """List the files a run moved into quarantine.

    Returns
    -------
    list[Path]
        The quarantine entries.
    """
    directory = _layout(tmp_path).quarantine_dir
    return sorted(directory.iterdir()) if directory.exists() else []


def test_archiving_publishes_and_releases_the_source(tmp_path: Path) -> None:
    """A verified archive, its sidecar and the removal of the source."""
    data = _document(_records(2))
    source = _source(tmp_path, 1, data)
    context = _context(tmp_path)

    result = archive_source(context, _sealed_entry(source, 1))

    archive = context.layout.archive_dir / archive_name(1)
    assert result.action == "archived", "the source becomes an archive"
    assert result.logical_bytes == len(data), "the sidecar's logical size is reported"
    assert result.physical_bytes == archive.stat().st_size, "the XZ size is reported"
    assert lzma.decompress(archive.read_bytes()) == data, "the rows survive intact"
    assert not source.exists(), "the source is released"
    assert not (context.layout.sealed_dir / sealed_meta_name(1)).exists(), (
        "the writer's metadata goes with the source"
    )
    archived = read_inventory(context.layout).archived[0]
    assert archived.has_metadata, "the published sidecar is trusted"
    assert archived.record_count == 2, "the sidecar's count is available"


def test_the_configured_preset_is_recorded(tmp_path: Path) -> None:
    """The sidecar names the preset that produced the archive."""
    source = _source(tmp_path, 1, _document(_records(1)))
    context = _context(tmp_path, preset=3)

    archive_source(context, _sealed_entry(source, 1))

    sidecar = (context.layout.archive_dir / sidecar_name(1)).read_text()
    assert '"xz_preset":3' in sidecar, "the preset is recorded for later rewrites"


def test_an_existing_archive_is_never_clobbered(tmp_path: Path) -> None:
    """A source beside a published archive is reconciliation's problem."""
    data = _document(_records(1))
    source = _source(tmp_path, 1, data)
    context = _context(tmp_path)
    destination = context.layout.archive_dir / archive_name(1)
    destination.write_bytes(b"published")

    with pytest.raises(FileExistsError):
        archive_source(context, _sealed_entry(source, 1))

    assert destination.read_bytes() == b"published", "the archive is untouched"
    assert source.exists(), "the source is kept for the next run"
    assert not list(context.layout.archive_dir.glob(".*.tmp")), (
        "the abandoned temporary is removed"
    )


def test_a_torn_tail_is_dropped_and_reported(tmp_path: Path) -> None:
    """A crash-truncated final row never enters a published archive."""
    complete = _document(_records(1))
    source = _source(tmp_path, 1, complete + b'{"kind": "outc')
    context = _context(tmp_path)

    archive_source(context, _sealed_entry(source, 1))

    archive = context.layout.archive_dir / archive_name(1)
    assert lzma.decompress(archive.read_bytes()) == complete, (
        "only complete rows are published"
    )
    assert (
        '"had_torn_tail":true'
        in (context.layout.archive_dir / sidecar_name(1)).read_text()
    ), "the tear is recorded"


def test_a_corrupt_source_is_not_archived(tmp_path: Path) -> None:
    """A terminated invalid row fails the source before publication."""
    source = _source(tmp_path, 1, _document(_records(1)) + b'{"kind": "unknown"}\n')
    context = _context(tmp_path)

    with pytest.raises(SegmentFaultError) as raised:
        archive_source(context, _sealed_entry(source, 1))

    assert raised.value.reason == "corrupt", "the fault is closed"
    assert not (context.layout.archive_dir / archive_name(1)).exists(), (
        "no archive is published"
    )
    assert source.exists(), "the source is left for quarantine"


def test_an_unsupported_version_is_named(tmp_path: Path) -> None:
    """A future record schema is reported as such, not as corruption."""
    document = (
        b'{"format":"buildlimits-cache-outcomes","version":2,'
        b'"salt_id":"salt-1","written_at":"2026-11-19T00:00:00Z"}\n'
    )
    source = _source(tmp_path, 1, document)

    with pytest.raises(SegmentFaultError) as raised:
        archive_source(_context(tmp_path), _sealed_entry(source, 1))

    assert raised.value.reason == "version_mismatch", "the schema fault is distinct"


def test_a_trim_rewrites_the_archive_and_its_sidecar(tmp_path: Path) -> None:
    """A prefix trim drops old rows and republishes exact facts."""
    data = _document(_records(4))
    source = _source(tmp_path, 1, data)
    context = _context(tmp_path)
    archive_source(context, _sealed_entry(source, 1))
    entry = _archived_entry(tmp_path, 1)
    cutoff = NOW - dt.timedelta(minutes=1)

    result = recompress_archive(context, entry, Trim(before=cutoff))

    archived = lzma.decompress(entry.path.read_bytes())
    assert result.action == "trimmed", "the archive is rewritten in place"
    assert archived.count(b"\n") == 3, "the header and two retained rows remain"
    assert result.logical_bytes == len(archived), "the sidecar matches the archive"
    assert read_archive_facts(entry.path).first_observed_at == cutoff, (
        "the retained history starts at the cutoff"
    )


def test_a_trim_that_keeps_no_records_deletes_the_archive(tmp_path: Path) -> None:
    """An empty segment carries no history and is removed."""
    source = _source(tmp_path, 1, _document(_records(2)))
    context = _context(tmp_path)
    archive_source(context, _sealed_entry(source, 1))
    entry = _archived_entry(tmp_path, 1)

    result = recompress_archive(context, entry, Trim(records=2))

    assert result.action == "deleted", "the empty archive is a deletion"
    assert not entry.path.exists(), "the archive is gone"
    assert not (context.layout.archive_dir / sidecar_name(1)).exists(), (
        "the sidecar is gone with it"
    )


def test_a_corrupt_archive_is_not_rewritten(tmp_path: Path) -> None:
    """A rewrite reads and verifies its source before replacing it."""
    source = _source(tmp_path, 1, _document(_records(1)))
    context = _context(tmp_path)
    archive_source(context, _sealed_entry(source, 1))
    entry = _archived_entry(tmp_path, 1)
    entry.path.write_bytes(b"not xz at all")

    with pytest.raises(SegmentFaultError) as raised:
        recompress_archive(context, entry, Trim(records=1))

    assert raised.value.reason == "bad_xz", "the encoding fault is closed"
    assert entry.path.read_bytes() == b"not xz at all", "the damaged file is untouched"


def test_repairing_a_sidecar_rebuilds_it_from_the_archive(tmp_path: Path) -> None:
    """A missing sidecar is rederived from the bytes actually stored."""
    data = _document(_records(2))
    source = _source(tmp_path, 1, data)
    context = _context(tmp_path)
    archive_source(context, _sealed_entry(source, 1))
    entry = _archived_entry(tmp_path, 1)
    (context.layout.archive_dir / sidecar_name(1)).unlink()

    result = repair_sidecar(context, entry)

    sidecar = (context.layout.archive_dir / sidecar_name(1)).read_text()
    assert result.action == "repaired", "the sidecar is rebuilt"
    assert result.logical_bytes == len(data), "the verified size is reported"
    assert '"record_count":2' in sidecar, "the archive's own rows are counted"


def test_a_matching_duplicate_is_reconciled(tmp_path: Path) -> None:
    """A source whose archive proves the same content is redundant."""
    data = _document(_records(2))
    source = _source(tmp_path, 1, data)
    context = _context(tmp_path)
    archive_source(context, _sealed_entry(source, 1))
    archived = _archived_entry(tmp_path, 1)
    source.write_bytes(data)
    source.chmod(0o600)

    result = reconcile_duplicate(context, _sealed_entry(source, 1), archived)

    assert result.action == "reconciled", "the pair is settled as one archive"
    assert not source.exists(), "the redundant source is removed"
    assert archived.path.exists(), "the verified archive stays"
    assert _quarantined(tmp_path) == [], "a matching pair needs no quarantine"


def test_a_mismatched_archive_is_replaced_from_its_source(tmp_path: Path) -> None:
    """A stale archive is quarantined and the source archived again."""
    source_data = _document(_records(2, first=1))
    older = _document(_records(1, first=1))
    source = _source(tmp_path, 1, older)
    context = _context(tmp_path)
    archive_source(context, _sealed_entry(source, 1))
    archived = _archived_entry(tmp_path, 1)
    source.write_bytes(source_data)
    source.chmod(0o600)

    result = reconcile_duplicate(context, _sealed_entry(source, 1), archived)

    assert result.action == "re-archived", "the source is the authority"
    assert lzma.decompress(archived.path.read_bytes()) == source_data, (
        "the archive now matches the source"
    )
    assert any(".mismatch." in path.name for path in _quarantined(tmp_path)), (
        "the stale archive is quarantined as a mismatch"
    )


def test_an_unreadable_archive_is_replaced_from_its_source(tmp_path: Path) -> None:
    """An archive that will not verify is quarantined before re-archiving."""
    data = _document(_records(1))
    source = _source(tmp_path, 1, data)
    context = _context(tmp_path)
    archive_source(context, _sealed_entry(source, 1))
    archived = _archived_entry(tmp_path, 1)
    archived.path.write_bytes(b"not xz at all")
    source.write_bytes(data)
    source.chmod(0o600)

    result = reconcile_duplicate(context, _sealed_entry(source, 1), archived)

    assert result.action == "re-archived", "the source is archived again"
    assert lzma.decompress(archived.path.read_bytes()) == data, "the archive is valid"
    assert ".bad_xz." in _quarantined(tmp_path)[0].name, "the fault is recorded"


def test_a_corrupt_source_is_quarantined_and_the_archive_kept(
    tmp_path: Path,
) -> None:
    """A damaged source cannot displace an archive that still verifies."""
    data = _document(_records(1))
    source = _source(tmp_path, 1, data)
    context = _context(tmp_path)
    archive_source(context, _sealed_entry(source, 1))
    archived = _archived_entry(tmp_path, 1)
    source.write_bytes(b"not json")
    source.chmod(0o600)

    result = reconcile_duplicate(context, _sealed_entry(source, 1), archived)

    assert result.action == "quarantined", "the damaged source is removed"
    assert result.reason == "corrupt", "its reason is closed"
    assert not source.exists(), "the source is quarantined"
    assert archived.path.exists(), "the verified archive is kept"
    assert ".corrupt." in _quarantined(tmp_path)[0].name, "the fault is recorded"


def test_deleting_an_archive_removes_its_sidecar(tmp_path: Path) -> None:
    """Expired history leaves no sidecar that would describe nothing."""
    source = _source(tmp_path, 1, _document(_records(1)))
    context = _context(tmp_path)
    archive_source(context, _sealed_entry(source, 1))
    entry = _archived_entry(tmp_path, 1)

    delete_archive(context, entry)

    assert not entry.path.exists(), "the archive is gone"
    assert not (context.layout.archive_dir / sidecar_name(1)).exists(), (
        "the sidecar is gone"
    )


def test_an_orphan_sidecar_is_quarantined(tmp_path: Path) -> None:
    """A sidecar with no archive is never silently deleted."""
    context = _context(tmp_path)
    orphan = context.layout.archive_dir / sidecar_name(2)
    orphan.write_text("{}")

    result = remove_orphan_sidecar(context, 2)

    assert result.action == "quarantined", "the orphan is moved aside"
    assert result.reason == "orphan_sidecar", "its reason is closed"
    assert not orphan.exists(), "nothing describes a missing archive"
    assert ".orphan_sidecar." in _quarantined(tmp_path)[0].name, (
        "the quarantine name records why"
    )


def test_quarantine_never_replaces_an_earlier_entry(tmp_path: Path) -> None:
    """Two damaged files sharing a name keep separate quarantine entries."""
    context = _context(tmp_path)
    first = context.layout.archive_dir / "first" / archive_name(1)
    second = context.layout.archive_dir / "second" / archive_name(1)
    for path, data in ((first, b"one"), (second, b"two")):
        path.parent.mkdir()
        path.write_bytes(data)
        path.chmod(0o600)

    quarantine_archive(context, _sealed_entry(first, 1), "bad_xz")
    quarantine_archive(context, _sealed_entry(second, 1), "bad_xz")

    names = [path.name for path in _quarantined(tmp_path)]
    assert len(names) == 2, "both files survive"
    assert len(set(names)) == 2, "the quarantine names never collide"


def test_quarantining_an_archive_drops_its_sidecar(tmp_path: Path) -> None:
    """A quarantined archive must not leave a sidecar behind."""
    context = _context(tmp_path)
    archive = context.layout.archive_dir / archive_name(1)
    archive.write_bytes(b"junk")
    (context.layout.archive_dir / sidecar_name(1)).write_text("{}")

    result = quarantine_archive(context, _sealed_entry(archive, 1), "bad_xz")

    assert result.reason == "bad_xz", "the fault reason is reported"
    assert not (context.layout.archive_dir / sidecar_name(1)).exists(), (
        "the sidecar is gone"
    )
    assert len(_quarantined(tmp_path)) == 1, "the archive is moved aside"


def test_source_and_archive_facts_describe_the_same_rows(tmp_path: Path) -> None:
    """Compression preserves the facts the source proved."""
    data = _document(_records(3))
    source = _source(tmp_path, 1, data)
    context = _context(tmp_path)
    source_facts = read_source_facts(source)

    archive_source(context, _sealed_entry(source, 1))
    archive = context.layout.archive_dir / archive_name(1)

    assert read_archive_facts(archive).matches(source_facts), (
        "count, size and digest agree"
    )


def test_recompressing_an_unreadable_archive_reports_its_reason(
    tmp_path: Path,
) -> None:
    """A truncation fault is distinguished from arbitrary garbage."""
    data = _document(_records(1))
    source = _source(tmp_path, 1, data)
    context = _context(tmp_path)
    archive_source(context, _sealed_entry(source, 1))
    entry = _archived_entry(tmp_path, 1)
    entry.path.write_bytes(entry.path.read_bytes()[:-8])

    with pytest.raises(SegmentFaultError) as raised:
        recompress_archive(context, entry, Trim(records=1))

    assert raised.value.reason == "truncated_xz", "the stream end is enforced"
