"""Segment naming, sidecar validation and the read-only inventory.

The inventory is what the planner trusts instead of reading segment content,
so its view of a damaged, duplicated or orphaned store is a safety boundary.
These tests pin the names, the closed metadata checks and every integrity flag
an operator sees in a report.
"""

import datetime as dt
import json
from pathlib import Path

import pytest

from buildlimits.outcome_segment_inventory import (
    Inventory,
    SegmentEntry,
    read_inventory,
)
from buildlimits.outcome_segment_sidecar import SIDECAR_SCHEMA, Sidecar
from buildlimits.outcome_segment_state import read_retention_floor, read_sequence
from buildlimits.outcome_segments import (
    ARCHIVE_DIR_NAME,
    SegmentLayout,
    archive_name,
    sealed_meta_name,
    segment_name,
    sidecar_name,
)

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)
_DIGEST = "a" * 64


def _layout(root: Path) -> SegmentLayout:
    """Derive the layout for one store below ``root``.

    Returns
    -------
    SegmentLayout
        The sibling paths the writer and archiver share.
    """
    return SegmentLayout.beside(root / "cache-outcomes.jsonl")


def _write(path: Path, data: bytes) -> Path:
    """Create one private file, making its parent directory first.

    Returns
    -------
    Path
        The written path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _sidecar_document(sequence: int, **overrides: object) -> dict[str, object]:
    """Build the JSON document for one valid sidecar.

    Returns
    -------
    dict[str, object]
        The sidecar mapping, optionally overridden by the caller.
    """
    document = Sidecar(
        sequence=sequence,
        salt_id="salt-1",
        record_count=2,
        logical_bytes=100,
        physical_bytes=40,
        sha256=_DIGEST,
        first_observed_at=NOW - dt.timedelta(minutes=1),
        last_observed_at=NOW,
        archived_at=NOW,
        xz_preset=1,
        had_torn_tail=False,
    ).as_dict()
    document.update(overrides)
    return document


def _write_sidecar(root: Path, sequence: int, **overrides: object) -> Path:
    """Write one sidecar document beside a presumed archive.

    Returns
    -------
    Path
        The sidecar path.
    """
    document = _sidecar_document(sequence, **overrides)
    return _write(
        root / ARCHIVE_DIR_NAME / sidecar_name(sequence),
        json.dumps(document).encode(),
    )


def test_names_sort_in_append_order() -> None:
    """Zero padding makes a directory listing follow sequence order."""
    names = [segment_name(sequence) for sequence in (10, 2, 1)]

    assert sorted(names) == [
        segment_name(1),
        segment_name(2),
        segment_name(10),
    ], "a lexical listing is an append-order listing"


def test_derived_names_extend_the_segment_name() -> None:
    """Every derived name is the segment name plus one closed suffix."""
    name = segment_name(7)

    assert name == "segment-000000000007.jsonl", "the sequence is zero padded"
    assert archive_name(7) == f"{name}.xz", "an archive names its source"
    assert sidecar_name(7) == f"{name}.xz.json", "the sidecar names its archive"
    assert sealed_meta_name(7) == f"{name}.meta.json", "metadata names its source"


def test_layout_places_state_beside_the_active_store(tmp_path: Path) -> None:
    """The layout derives siblings without creating anything."""
    active = tmp_path / "cache-outcomes.jsonl"
    layout = SegmentLayout.beside(active)

    assert layout.active == active, "the active path is unchanged"
    assert layout.sealed_dir.name == "cache-outcomes.sealed", "sources are private"
    assert layout.archive_dir.name == ARCHIVE_DIR_NAME, "archives are private"
    assert layout.sequence_file.name == "cache-outcomes.seq", "the counter is shared"
    assert layout.retention_file.parent == layout.archive_dir, (
        "retention state belongs to the archiver"
    )
    assert layout.quarantine_dir.parent == layout.archive_dir, (
        "quarantine is inside the archive directory"
    )
    assert not layout.archive_dir.exists(), "deriving the layout creates nothing"


def test_sequence_counter_accepts_surrounding_whitespace(tmp_path: Path) -> None:
    """A counter written with a trailing newline still reads as one integer."""
    path = _write(tmp_path / "cache-outcomes.seq", b" 12 \n")

    assert read_sequence(path) == 12, "whitespace is not part of the value"


def test_a_missing_counter_is_not_a_fault(tmp_path: Path) -> None:
    """An unwritten counter means the store has never sealed."""
    assert read_sequence(tmp_path / "absent.seq") is None, "absence is not an error"


@pytest.mark.parametrize(
    "data",
    [b"not-a-number", b"12x", b"", b"1" * 40, b"\xff\xfe"],
    ids=["letters", "trailing-garbage", "empty", "oversized", "non-ascii"],
)
def test_a_counter_that_is_not_one_decimal_integer_is_a_fault(
    tmp_path: Path, data: bytes
) -> None:
    """Anything but a bounded decimal integer is rejected, never guessed."""
    path = _write(tmp_path / "cache-outcomes.seq", data)

    with pytest.raises(ValueError, match="sequence counter"):
        read_sequence(path)


def test_a_directory_is_not_a_sequence_counter(tmp_path: Path) -> None:
    """A non-regular file cannot describe the next sequence."""
    (tmp_path / "cache-outcomes.seq").mkdir()

    with pytest.raises(ValueError, match="not a regular file"):
        read_sequence(tmp_path / "cache-outcomes.seq")


def test_retention_floor_is_read_from_state(tmp_path: Path) -> None:
    """The last successful run's floor survives a later read."""
    layout = _layout(tmp_path)
    _write(layout.retention_file, b'{"floor_sequence": 4}')

    assert read_retention_floor(layout) == 4, "the recorded floor is reported"


@pytest.mark.parametrize(
    "document",
    [
        b'{"floor_sequence": -1}',
        b'{"floor_sequence": true}',
        b'{"floor_sequence": "4"}',
        b'{"other": 4}',
        b"not json",
        b"[]",
    ],
    ids=["negative", "boolean", "text", "missing", "malformed", "sequence"],
)
def test_unusable_retention_state_is_ignored(tmp_path: Path, document: bytes) -> None:
    """A damaged floor never suppresses real gap diagnostics."""
    layout = _layout(tmp_path)
    _write(layout.retention_file, document)

    assert read_retention_floor(layout) is None, "no floor is trusted from damage"


def test_an_empty_store_has_an_empty_inventory(tmp_path: Path) -> None:
    """Absent directories describe no history and no faults."""
    inventory = read_inventory(_layout(tmp_path))

    assert inventory == Inventory(), "every field stays at its empty default"
    assert not inventory.effective, "no entry is effective"
    assert inventory.highest_sequence is None, "no closed sequence exists"


def test_sealed_metadata_describes_its_source(tmp_path: Path) -> None:
    """Writer metadata supplies counts and the observed range."""
    layout = _layout(tmp_path)
    _write(layout.sealed_dir / segment_name(1), b"source bytes")
    _write(
        layout.sealed_dir / sealed_meta_name(1),
        json.dumps({
            "sequence": 1,
            "salt_id": "salt-1",
            "record_count": 3,
            "first_observed_at": "2026-11-19T00:00:00Z",
            "last_observed_at": "2026-11-19T00:10:00Z",
        }).encode(),
    )

    entry = read_inventory(layout).sealed[0]

    assert entry.record_count == 3, "the count comes from metadata"
    assert entry.salt_id == "salt-1", "the domain comes from metadata"
    assert entry.first_observed_at == NOW, "the first moment is parsed"
    assert entry.last_observed_at == NOW + dt.timedelta(minutes=10), (
        "the last moment is parsed"
    )
    assert entry.has_metadata, "the entry says its counts are trustworthy"


def test_unusable_sealed_metadata_is_not_trusted(tmp_path: Path) -> None:
    """A malformed metadata document leaves the source usable but unnamed."""
    layout = _layout(tmp_path)
    _write(layout.sealed_dir / segment_name(1), b"source bytes")
    _write(
        layout.sealed_dir / sealed_meta_name(1),
        b'{"record_count": -1, "salt_id": ""}',
    )

    entry = read_inventory(layout).sealed[0]

    assert entry.record_count is None, "an invalid count is withheld"
    assert not entry.has_metadata, "the entry does not claim metadata"


def test_a_sidecar_supplies_the_logical_size(tmp_path: Path) -> None:
    """The byte budget counts sidecar logical bytes, not XZ bytes."""
    layout = _layout(tmp_path)
    _write(layout.archive_dir / archive_name(1), b"x" * 40)
    _write_sidecar(tmp_path, 1)

    inventory = read_inventory(layout)
    entry = inventory.archived[0]

    assert entry.physical_bytes == 40, "the archive's own size is measured"
    assert entry.logical_bytes == 100, "the sidecar's logical size is used"
    assert entry.record_count == 2, "the sidecar's count is reported"
    assert entry.sha256 == _DIGEST, "the verified digest is reported"
    assert inventory.archived_logical_bytes == 100, "the budget sees logical bytes"


def test_an_archive_without_a_sidecar_is_flagged(tmp_path: Path) -> None:
    """A missing sidecar is reported, not silently treated as zero bytes."""
    layout = _layout(tmp_path)
    _write(layout.archive_dir / archive_name(1), b"x" * 40)

    inventory = read_inventory(layout)

    assert inventory.missing_sidecars == (1,), "the gap is named"
    assert inventory.archived[0].logical_bytes == 40, "physical bytes are the fallback"


@pytest.mark.parametrize(
    "document",
    [
        b'{"schema": 2}',
        b'{"schema": 1, "sequence": 9}',
        b"not json",
        b"[]",
    ],
    ids=["schema", "wrong-sequence", "malformed", "sequence"],
)
def test_a_corrupt_sidecar_is_flagged(tmp_path: Path, document: bytes) -> None:
    """A sidecar that disagrees with its archive is never trusted."""
    layout = _layout(tmp_path)
    _write(layout.archive_dir / archive_name(1), b"x" * 40)
    _write(layout.archive_dir / sidecar_name(1), document)

    inventory = read_inventory(layout)

    assert inventory.corrupt_sidecars == (1,), "the damaged sidecar is named"
    assert not inventory.archived[0].has_metadata, "its fields are withheld"


@pytest.mark.parametrize(
    "overrides",
    [
        {"sha256": "abc"},
        {"sha256": "g" * 64},
        {"logical_bytes": -1},
        {"archived_at": None},
        {"salt_id": ""},
        {"first_observed_at": 5},
    ],
    ids=["short-digest", "non-hex", "negative", "no-moment", "no-salt", "bad-moment"],
)
def test_a_sidecar_field_must_hold_a_usable_value(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    """Every required field is validated before its value reaches a report."""
    layout = _layout(tmp_path)
    _write(layout.archive_dir / archive_name(1), b"x" * 40)
    _write_sidecar(tmp_path, 1, **overrides)

    assert read_inventory(layout).corrupt_sidecars == (1,), "the value is rejected"


def test_a_sidecar_behind_a_directory_is_corrupt(tmp_path: Path) -> None:
    """An unreadable sidecar path is damage, not absence."""
    layout = _layout(tmp_path)
    _write(layout.archive_dir / archive_name(1), b"x" * 40)
    (layout.archive_dir / sidecar_name(1)).mkdir()

    assert read_inventory(layout).corrupt_sidecars == (1,), "the read failure is closed"


def test_an_orphan_sidecar_is_flagged(tmp_path: Path) -> None:
    """A sidecar with no archive describes nothing and is reported."""
    layout = _layout(tmp_path)
    _write_sidecar(tmp_path, 2)

    inventory = read_inventory(layout)

    assert inventory.orphan_sidecars == (2,), "the orphan is named"
    assert not inventory.archived, "an orphan is not an archive"


def test_a_duplicate_prefers_the_sealed_source(tmp_path: Path) -> None:
    """A source that outlived a crash is the authority over its archive."""
    layout = _layout(tmp_path)
    _write(layout.sealed_dir / segment_name(1), b"source bytes")
    _write(layout.archive_dir / archive_name(1), b"x" * 40)
    _write_sidecar(tmp_path, 1)

    inventory = read_inventory(layout)

    assert len(inventory.archived) == 1, "the archive is still inventoried"
    assert [entry.state for entry in inventory.effective] == ["sealed"], (
        "one sequence is read once, from the source"
    )
    assert inventory.highest_sequence == 1, "the duplicate is one sequence"


def test_gaps_start_at_the_retention_floor(tmp_path: Path) -> None:
    """Sequences below the floor were evicted on purpose, not lost."""
    layout = _layout(tmp_path)
    for sequence in (1, 4):
        _write(layout.sealed_dir / segment_name(sequence), b"rows")

    assert read_inventory(layout).gaps == (2, 3), "the missing middle is named"
    assert read_inventory(layout, retention_floor=3).gaps == (3,), (
        "an intentionally evicted prefix is not a gap"
    )
    assert not read_inventory(layout, retention_floor=5).gaps, (
        "a floor above every sequence leaves nothing missing"
    )


def test_inventory_byte_totals_follow_state(tmp_path: Path) -> None:
    """Sealed sources count physically; archives count logically."""
    layout = _layout(tmp_path)
    _write(layout.sealed_dir / segment_name(1), b"s" * 30)
    _write(layout.archive_dir / archive_name(2), b"x" * 40)
    _write_sidecar(tmp_path, 2)

    inventory = read_inventory(layout)

    assert inventory.sealed_bytes == 30, "sealed bytes are the source's size"
    assert inventory.archived_logical_bytes == 100, "archived bytes are logical"
    assert inventory.highest_sequence == 2, "the highest closed sequence is known"


def test_sidecar_json_is_closed_and_stable() -> None:
    """The sidecar schema names every field a later run depends on."""
    document = _sidecar_document(3)

    assert document["schema"] == SIDECAR_SCHEMA, "the schema number is carried"
    assert document["sequence"] == 3, "the sequence is carried"
    assert document["format_version"] == 1, "the record schema is preserved"
    assert document["xz_check"] == "crc64", "the integrity check is named"
    assert json.loads(json.dumps(document, sort_keys=True)) == document, (
        "the sidecar is plain JSON"
    )


def test_sealed_entries_carry_their_path_and_state(tmp_path: Path) -> None:
    """An entry names exactly the file and state that own it."""
    layout = _layout(tmp_path)
    path = _write(layout.sealed_dir / segment_name(1), b"rows")

    entry = read_inventory(layout).sealed[0]

    assert entry == SegmentEntry(
        sequence=1, path=path, state="sealed", physical_bytes=4, logical_bytes=4
    ), "the entry describes the source without reading it"
