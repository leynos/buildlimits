"""Archiving, trimming and sidecar repair for one sealed segment.

Each operation here runs under the archive lock and may publish or rewrite a
file. The tests drive the real filesystem so a failure is observed the way the
next timer firing would see it: the source still readable, a published sidecar
describing exactly the bytes that were verified, and a damaged file left for
quarantine rather than rewritten.
"""

import datetime as dt
import json
import lzma
import typing as typ

import pytest
from archive_files_helpers import (
    NOW,
    archived_entry,
    context,
    document,
    records,
    sealed_entry,
    source,
)

from buildlimits import outcome_archive_encode, outcome_archive_files
from buildlimits.outcome_archive_encode import read_archive_facts, read_source_facts
from buildlimits.outcome_archive_files import (
    archive_source,
    recompress_archive,
    repair_sidecar,
)
from buildlimits.outcome_archive_verify import (
    OutputFaultError,
    SegmentFaultError,
    Trim,
)
from buildlimits.outcome_segment_inventory import read_inventory
from buildlimits.outcome_segments import (
    archive_name,
    replacing_name,
    sealed_meta_name,
    sidecar_name,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

# The sidecar's whole key set is a contract every reader shares, so the test
# names it once and compares sets rather than restating a large literal inline.
_SIDECAR_FIELDS = frozenset({
    "schema",
    "sequence",
    "format",
    "format_version",
    "salt_id",
    "record_count",
    "logical_bytes",
    "physical_bytes",
    "sha256",
    "first_observed_at",
    "last_observed_at",
    "archived_at",
    "xz_preset",
    "xz_check",
    "had_torn_tail",
})


def test_archiving_publishes_and_releases_the_source(tmp_path: Path) -> None:
    """A verified archive, its sidecar and the removal of the source."""
    data = document(records(2))
    sealed = source(tmp_path, 1, data)
    run = context(tmp_path)

    result = archive_source(run, sealed_entry(sealed, 1))

    archive = run.layout.archive_dir / archive_name(1)
    assert result.action == "archived", "the source becomes an archive"
    assert result.logical_bytes == len(data), "the sidecar's logical size is reported"
    assert result.physical_bytes == archive.stat().st_size, "the XZ size is reported"
    assert lzma.decompress(archive.read_bytes()) == data, "the rows survive intact"
    assert not sealed.exists(), "the source is released"
    assert not (run.layout.sealed_dir / sealed_meta_name(1)).exists(), (
        "the writer's metadata goes with the source"
    )
    archived = read_inventory(run.layout).archived[0]
    assert archived.has_metadata, "the published sidecar is trusted"
    assert archived.record_count == 2, "the sidecar's count is available"


def test_a_published_sidecar_has_exactly_the_documented_fields(
    tmp_path: Path,
) -> None:
    """The sidecar's key set is a contract shared by every reader."""
    sealed = source(tmp_path, 1, document(records(1)))
    run = context(tmp_path)

    archive_source(run, sealed_entry(sealed, 1))

    sidecar = json.loads((run.layout.archive_dir / sidecar_name(1)).read_bytes())
    assert set(sidecar) == _SIDECAR_FIELDS, "the sidecar schema is closed"


def test_the_configured_preset_is_recorded(tmp_path: Path) -> None:
    """The sidecar names the preset that produced the archive."""
    sealed = source(tmp_path, 1, document(records(1)))
    run = context(tmp_path, preset=3)

    archive_source(run, sealed_entry(sealed, 1))

    sidecar = (run.layout.archive_dir / sidecar_name(1)).read_text()
    assert '"xz_preset":3' in sidecar, "the preset is recorded for later rewrites"


def test_an_existing_archive_is_never_clobbered(tmp_path: Path) -> None:
    """A source beside a published archive is reconciliation's problem."""
    data = document(records(1))
    sealed = source(tmp_path, 1, data)
    run = context(tmp_path)
    destination = run.layout.archive_dir / archive_name(1)
    destination.write_bytes(b"published")

    with pytest.raises(FileExistsError):
        archive_source(run, sealed_entry(sealed, 1))

    assert destination.read_bytes() == b"published", "the archive is untouched"
    assert sealed.exists(), "the source is kept for the next run"
    assert not list(run.layout.archive_dir.glob(".*.tmp")), (
        "the abandoned temporary is removed"
    )


def test_a_torn_tail_is_dropped_and_reported(tmp_path: Path) -> None:
    """A crash-truncated final row never enters a published archive."""
    complete = document(records(1))
    sealed = source(tmp_path, 1, complete + b'{"kind": "outc')
    run = context(tmp_path)

    archive_source(run, sealed_entry(sealed, 1))

    archive = run.layout.archive_dir / archive_name(1)
    assert lzma.decompress(archive.read_bytes()) == complete, (
        "only complete rows are published"
    )
    assert (
        '"had_torn_tail":true' in (run.layout.archive_dir / sidecar_name(1)).read_text()
    ), "the tear is recorded"


def test_a_corrupt_source_is_not_archived(tmp_path: Path) -> None:
    """A terminated invalid row fails the source before publication."""
    sealed = source(tmp_path, 1, document(records(1)) + b'{"kind": "unknown"}\n')
    run = context(tmp_path)

    with pytest.raises(SegmentFaultError) as raised:
        archive_source(run, sealed_entry(sealed, 1))

    assert raised.value.reason == "corrupt", "the fault is closed"
    assert not (run.layout.archive_dir / archive_name(1)).exists(), (
        "no archive is published"
    )
    assert sealed.exists(), "the source is left for quarantine"


def test_an_unsupported_version_is_named(tmp_path: Path) -> None:
    """A future record schema is reported as such, not as corruption."""
    raw = (
        b'{"format":"buildlimits-cache-outcomes","version":2,'
        b'"salt_id":"salt-1","written_at":"2026-11-19T00:00:00Z"}\n'
    )
    sealed = source(tmp_path, 1, raw)

    with pytest.raises(SegmentFaultError) as raised:
        archive_source(context(tmp_path), sealed_entry(sealed, 1))

    assert raised.value.reason == "version_mismatch", "the schema fault is distinct"


def test_a_trim_rewrites_the_archive_and_its_sidecar(tmp_path: Path) -> None:
    """A prefix trim drops old rows and republishes exact facts."""
    data = document(records(4))
    sealed = source(tmp_path, 1, data)
    run = context(tmp_path)
    archive_source(run, sealed_entry(sealed, 1))
    entry = archived_entry(tmp_path)
    cutoff = NOW - dt.timedelta(minutes=1)

    result = recompress_archive(run, entry, Trim(before=cutoff))

    archived = lzma.decompress(entry.path.read_bytes())
    assert result.action == "trimmed", "the archive is rewritten in place"
    assert archived.count(b"\n") == 3, "the header and two retained rows remain"
    assert result.logical_bytes == len(archived), "the sidecar matches the archive"
    assert read_archive_facts(entry.path).first_observed_at == cutoff, (
        "the retained history starts at the cutoff"
    )


def test_a_trim_that_keeps_no_records_deletes_the_archive(tmp_path: Path) -> None:
    """An empty segment carries no history and is removed."""
    sealed = source(tmp_path, 1, document(records(2)))
    run = context(tmp_path)
    archive_source(run, sealed_entry(sealed, 1))
    entry = archived_entry(tmp_path)

    result = recompress_archive(run, entry, Trim(records=2))

    assert result.action == "deleted", "the empty archive is a deletion"
    assert not entry.path.exists(), "the archive is gone"
    assert not (run.layout.archive_dir / sidecar_name(1)).exists(), (
        "the sidecar is gone with it"
    )


def test_a_trim_without_its_sidecar_leaves_the_archive_for_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed metadata write removes the old sidecar instead of misdescribing.

    The replacement archive is already published when the sidecar is written.
    Keeping the previous sidecar would report the old counts beside the new
    bytes; removing it routes the archive to the heal path on the next run.
    """
    sealed = source(tmp_path, 1, document(records(4)))
    run = context(tmp_path)
    archive_source(run, sealed_entry(sealed, 1))
    entry = archived_entry(tmp_path)
    cutoff = NOW - dt.timedelta(minutes=1)

    def refuse(*_args: object, **_kwargs: object) -> object:
        """Fail the sidecar write so the trim keeps its archive for repair."""
        raise OSError("the sidecar cannot be written")

    monkeypatch.setattr(outcome_archive_files, "write_sidecar", refuse)

    with pytest.raises(OSError, match="cannot be written"):
        recompress_archive(run, entry, Trim(before=cutoff))

    assert lzma.decompress(entry.path.read_bytes()).count(b"\n") == 3, (
        "the trimmed archive is the one that stays published"
    )
    assert not (run.layout.archive_dir / sidecar_name(1)).exists(), (
        "no stale sidecar describes the previous bytes"
    )
    assert (run.layout.archive_dir / replacing_name(1)).exists(), (
        "the marker stays until a sidecar describes the new bytes"
    )
    assert read_inventory(run.layout).corrupt_sidecars == (1,), (
        "the next scan heals the archive from its own content"
    )


def test_a_rewrite_that_stops_before_its_sidecar_is_repaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between the rename and the sidecar is detected, not trusted.

    Equal compressed sizes cannot distinguish the old archive from its
    replacement, so the old sidecar is accepted by size alone. The marker left
    by the rewrite is what keeps that document from being believed.
    """
    sealed = source(tmp_path, 1, document(records(3)))
    run = context(tmp_path)
    archive_source(run, sealed_entry(sealed, 1))
    entry = archived_entry(tmp_path)

    def stop(*_args: object, **_kwargs: object) -> object:
        """Model a process that dies while writing the new sidecar."""
        raise KeyboardInterrupt

    monkeypatch.setattr(outcome_archive_files, "write_sidecar", stop)
    with pytest.raises(KeyboardInterrupt):
        recompress_archive(run, entry, Trim(records=1))
    monkeypatch.undo()

    damaged = read_inventory(run.layout)
    assert damaged.corrupt_sidecars == (1,), "the stale sidecar is not trusted"
    assert not damaged.archived[0].has_metadata, "no old count is reported"
    assert read_archive_facts(entry.path).record_count == 2, (
        "the published archive is the trimmed one"
    )

    archived = read_inventory(run.layout).archived[0]
    result = repair_sidecar(run, archived)

    assert result.action == "repaired", "the heal path rebuilds the sidecar"
    healed = read_inventory(run.layout)
    assert healed.corrupt_sidecars == (), "the archive is trusted again"
    assert healed.archived[0].record_count == 2, "the rebuilt count is the real one"


def test_repairing_after_a_failed_sidecar_write_reports_the_trimmed_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heal path re-derives the counts the lost write would have recorded."""
    sealed = source(tmp_path, 1, document(records(4)))
    run = context(tmp_path)
    archive_source(run, sealed_entry(sealed, 1))
    entry = archived_entry(tmp_path)
    cutoff = NOW - dt.timedelta(minutes=1)

    def refuse(*_args: object, **_kwargs: object) -> object:
        """Fail the sidecar write the trim performs after its rename."""
        raise OSError("the sidecar cannot be written")

    monkeypatch.setattr(outcome_archive_files, "write_sidecar", refuse)
    with pytest.raises(OSError, match="the sidecar cannot be written"):
        recompress_archive(run, entry, Trim(before=cutoff))
    monkeypatch.undo()

    result = repair_sidecar(run, entry)

    assert result.action == "repaired", "the sidecar is rebuilt"
    assert read_archive_facts(entry.path).record_count == 2, (
        "the rebuilt sidecar counts only the retained rows"
    )
    assert read_archive_facts(entry.path).first_observed_at == cutoff, (
        "the retained history starts at the cutoff"
    )


def test_a_corrupt_archive_is_not_rewritten(tmp_path: Path) -> None:
    """A rewrite reads and verifies its source before replacing it."""
    sealed = source(tmp_path, 1, document(records(1)))
    run = context(tmp_path)
    archive_source(run, sealed_entry(sealed, 1))
    entry = archived_entry(tmp_path)
    entry.path.write_bytes(b"not xz at all")

    with pytest.raises(SegmentFaultError) as raised:
        recompress_archive(run, entry, Trim(records=1))

    assert raised.value.reason == "bad_xz", "the encoding fault is closed"
    assert entry.path.read_bytes() == b"not xz at all", "the damaged file is untouched"


def test_repairing_a_sidecar_rebuilds_it_from_the_archive(tmp_path: Path) -> None:
    """A missing sidecar is rederived from the bytes actually stored."""
    data = document(records(2))
    sealed = source(tmp_path, 1, data)
    run = context(tmp_path)
    archive_source(run, sealed_entry(sealed, 1))
    entry = archived_entry(tmp_path)
    (run.layout.archive_dir / sidecar_name(1)).unlink()

    result = repair_sidecar(run, entry)

    sidecar = (run.layout.archive_dir / sidecar_name(1)).read_text()
    assert result.action == "repaired", "the sidecar is rebuilt"
    assert result.logical_bytes == len(data), "the verified size is reported"
    assert '"record_count":2' in sidecar, "the archive's own rows are counted"


def test_source_and_archive_facts_describe_the_same_rows(tmp_path: Path) -> None:
    """Compression preserves the facts the source proved."""
    data = document(records(3))
    sealed = source(tmp_path, 1, data)
    run = context(tmp_path)
    source_facts = read_source_facts(sealed)

    archive_source(run, sealed_entry(sealed, 1))
    archive = run.layout.archive_dir / archive_name(1)

    assert read_archive_facts(archive).matches(source_facts), (
        "count, size and digest agree"
    )


def test_recompressing_an_unreadable_archive_reports_its_reason(
    tmp_path: Path,
) -> None:
    """A truncation fault is distinguished from arbitrary garbage."""
    sealed = source(tmp_path, 1, document(records(1)))
    run = context(tmp_path)
    archive_source(run, sealed_entry(sealed, 1))
    entry = archived_entry(tmp_path)
    entry.path.write_bytes(entry.path.read_bytes()[:-8])

    with pytest.raises(SegmentFaultError) as raised:
        recompress_archive(run, entry, Trim(records=1))

    assert raised.value.reason == "truncated_xz", "the stream end is enforced"


def _refuse_scan(*_args: object, **_kwargs: object) -> object:
    """Fail the verification pass over the temporary archive."""
    raise SegmentFaultError("truncated_xz")


def test_a_temporary_that_will_not_decode_is_not_blamed_on_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source is readable, so a bad temporary must not quarantine it."""
    sealed = source(tmp_path, 1, document(records(2)))
    run = context(tmp_path)
    monkeypatch.setattr(outcome_archive_encode, "read_archive_facts", _refuse_scan)

    with pytest.raises(OutputFaultError) as raised:
        archive_source(run, sealed_entry(sealed, 1))

    assert raised.value.reason == "truncated_xz", "the fault names the output"
    assert sealed.exists(), "the readable source survives for the next attempt"
    assert not (run.layout.archive_dir / archive_name(1)).exists(), (
        "nothing unverified is published"
    )
    assert not list(run.layout.archive_dir.iterdir()), "the temporary is discarded"


def test_a_trim_whose_temporary_will_not_decode_keeps_the_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rewrite that cannot verify its own output leaves the archive intact."""
    sealed = source(tmp_path, 1, document(records(2)))
    run = context(tmp_path)
    archive_source(run, sealed_entry(sealed, 1))
    entry = archived_entry(tmp_path)
    before = entry.path.read_bytes()
    monkeypatch.setattr(outcome_archive_encode, "read_archive_facts", _refuse_scan)

    with pytest.raises(OutputFaultError) as raised:
        recompress_archive(run, entry, Trim(records=1))

    assert raised.value.reason == "truncated_xz", "the fault names the output"
    assert entry.path.read_bytes() == before, "the previous archive is untouched"
    assert read_inventory(run.layout).archived[0].has_metadata, (
        "its sidecar still describes the file that is published"
    )
