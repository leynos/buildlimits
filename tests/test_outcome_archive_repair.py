"""Trimming a published archive and repairing the sidecar it damaged.

A trim rewrites an archive in place and then replaces its sidecar. A crash or
a failed write between those two steps would leave a document describing the
previous bytes, so the tests here observe the states the next timer firing can
find and the repair that reconciles each one from the archive's own content.
"""

import datetime as dt
import lzma
import typing as typ
from pathlib import Path

import pytest
from archive_files_helpers import NOW, published

from buildlimits import outcome_archive_files
from buildlimits.outcome_archive_encode import read_archive_facts
from buildlimits.outcome_archive_files import recompress_archive, repair_sidecar
from buildlimits.outcome_archive_verify import SegmentFaultError, Trim
from buildlimits.outcome_segment_inventory import read_inventory
from buildlimits.outcome_segments import replacing_name, sidecar_name

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive_context import ArchiveContext
    from buildlimits.outcome_segment_inventory import SegmentEntry

_SIDECAR_ERROR = "the sidecar cannot be written"


def _trim_without_a_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    run: ArchiveContext,
    entry: SegmentEntry,
    cutoff: dt.datetime,
) -> None:
    """Trim an archive, failing the sidecar write that follows its rename.

    The replacement archive is already published when the sidecar is written,
    so refusing that write models the window the repair path reconciles.
    """

    def refuse(*_args: object, **_kwargs: object) -> object:
        """Fail the sidecar write the trim performs after its rename."""
        raise OSError(_SIDECAR_ERROR)

    monkeypatch.setattr(outcome_archive_files, "write_sidecar", refuse)
    with pytest.raises(OSError, match="cannot be written"):
        recompress_archive(run, entry, Trim(before=cutoff))


def test_a_trim_rewrites_the_archive_and_its_sidecar(tmp_path: Path) -> None:
    """A prefix trim drops old rows and republishes exact facts."""
    run, entry = published(tmp_path, 4)
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
    run, entry = published(tmp_path, 2)

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
    run, entry = published(tmp_path, 4)
    cutoff = NOW - dt.timedelta(minutes=1)
    _trim_without_a_sidecar(monkeypatch, run, entry, cutoff)

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
    run, entry = published(tmp_path, 3)

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
    assert not healed.corrupt_sidecars, "the archive is trusted again"
    assert healed.archived[0].record_count == 2, "the rebuilt count is the real one"


def test_repairing_after_a_failed_sidecar_write_reports_the_trimmed_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heal path re-derives the counts the lost write would have recorded."""
    run, entry = published(tmp_path, 4)
    cutoff = NOW - dt.timedelta(minutes=1)
    _trim_without_a_sidecar(monkeypatch, run, entry, cutoff)
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
    run, entry = published(tmp_path)
    entry.path.write_bytes(b"not xz at all")

    with pytest.raises(SegmentFaultError) as raised:
        recompress_archive(run, entry, Trim(records=1))

    assert raised.value.reason == "bad_xz", "the encoding fault is closed"
    assert entry.path.read_bytes() == b"not xz at all", "the damaged file is untouched"


def test_repairing_a_sidecar_rebuilds_it_from_the_archive(tmp_path: Path) -> None:
    """A missing sidecar is rederived from the bytes actually stored."""
    run, entry = published(tmp_path, 2)
    facts = read_archive_facts(entry.path)
    (run.layout.archive_dir / sidecar_name(1)).unlink()

    result = repair_sidecar(run, entry)

    sidecar = (run.layout.archive_dir / sidecar_name(1)).read_text()
    assert result.action == "repaired", "the sidecar is rebuilt"
    assert result.logical_bytes == facts.logical_bytes, "the verified size is reported"
    assert '"record_count":2' in sidecar, "the archive's own rows are counted"
