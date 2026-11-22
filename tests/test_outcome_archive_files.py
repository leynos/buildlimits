"""Publishing one verified archive and refusing every unverified one.

Each operation here runs under the archive lock and may publish a file. The
tests drive the real filesystem so a failure is observed the way the next
timer firing would see it: the source still readable, a published sidecar
describing exactly the bytes that were verified, and a damaged file left for
quarantine rather than rewritten by a pass that never proved its output.
"""

import dataclasses as dc
import json
import lzma
import typing as typ

import pytest
from archive_files_helpers import (
    context,
    document,
    published,
    records,
    sealed_entry,
    source,
)

from buildlimits import outcome_archive_encode
from buildlimits.outcome_archive_encode import read_archive_facts, read_source_facts
from buildlimits.outcome_archive_files import archive_source, recompress_archive
from buildlimits.outcome_archive_trim import Trim
from buildlimits.outcome_archive_verify import (
    ArchiveFacts,
    OutputFaultError,
    SegmentFaultError,
)
from buildlimits.outcome_segment_inventory import read_inventory
from buildlimits.outcome_segments import (
    archive_name,
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
    run, entry = published(tmp_path)
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


def test_a_temporary_that_does_not_match_its_facts_is_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A temporary that decompresses to other bytes never reaches its name.

    The publication gate compares what the writing pass recorded with what the
    temporary decompresses to. Only the comparison can catch an archive whose
    bytes changed after they were counted, so the injected mismatch models a
    corrupt encoder rather than a corrupt source.
    """
    other = source(tmp_path, 2, document(records(1)))
    run = context(tmp_path)
    archive_source(run, sealed_entry(other, 2))
    published = run.layout.archive_dir / archive_name(2)
    published_sidecar = run.layout.archive_dir / sidecar_name(2)
    archive_bytes = published.read_bytes()
    sidecar_bytes = published_sidecar.read_bytes()
    sealed = source(tmp_path, 1, document(records(2)))
    real_scan = read_archive_facts

    def mismatched(path: Path) -> ArchiveFacts:
        """Report the real facts under a digest the archive cannot hold."""
        return dc.replace(real_scan(path), sha256="0" * 64)

    monkeypatch.setattr(outcome_archive_encode, "read_archive_facts", mismatched)

    with pytest.raises(OutputFaultError) as raised:
        archive_source(run, sealed_entry(sealed, 1))

    assert raised.value.reason == "truncated_xz", "the fault names the output"
    assert not (run.layout.archive_dir / archive_name(1)).exists(), (
        "the mismatched temporary is never published"
    )
    assert not list(run.layout.archive_dir.glob(".*.tmp")), (
        "the mismatched temporary is discarded"
    )
    assert sealed.exists(), "the readable source survives for the next attempt"
    assert published.read_bytes() == archive_bytes, (
        "the archive already published is untouched"
    )
    assert published_sidecar.read_bytes() == sidecar_bytes, (
        "its sidecar still describes exactly those bytes"
    )


def test_a_trim_whose_temporary_will_not_decode_keeps_the_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rewrite that cannot verify its own output leaves the archive intact."""
    run, entry = published(tmp_path, 2)
    before = entry.path.read_bytes()
    monkeypatch.setattr(outcome_archive_encode, "read_archive_facts", _refuse_scan)

    with pytest.raises(OutputFaultError) as raised:
        recompress_archive(run, entry, Trim(records=1))

    assert raised.value.reason == "truncated_xz", "the fault names the output"
    assert entry.path.read_bytes() == before, "the previous archive is untouched"
    assert read_inventory(run.layout).archived[0].has_metadata, (
        "its sidecar still describes the file that is published"
    )
