"""The read-only inventory of sealed and archived segments.

The inventory is what the planner trusts instead of reading segment content,
so its view of a damaged, duplicated or orphaned store is a safety boundary.
These tests pin every integrity flag an operator sees in a report.
"""

import datetime as dt
import json
from pathlib import Path

import pytest
from segment_test_helpers import (
    DIGEST,
    NOW,
    sidecar_document,
    store_layout,
    write_private,
    write_sidecar,
)

from buildlimits.outcome_archive_dirs import UnsafePathError
from buildlimits.outcome_segment_gaps import MAX_GAP_SEQUENCES
from buildlimits.outcome_segment_inventory import (
    Inventory,
    SegmentEntry,
    read_inventory,
)
from buildlimits.outcome_segments import (
    archive_name,
    sealed_meta_name,
    segment_name,
    sidecar_name,
)


def test_an_empty_store_has_an_empty_inventory(tmp_path: Path) -> None:
    """Absent directories describe no history and no faults."""
    inventory = read_inventory(store_layout(tmp_path))

    assert inventory == Inventory(), "every field stays at its empty default"
    assert not inventory.effective, "no entry is effective"
    assert inventory.highest_sequence is None, "no closed sequence exists"


def test_sealed_metadata_describes_its_source(tmp_path: Path) -> None:
    """Writer metadata supplies counts and the observed range."""
    layout = store_layout(tmp_path)
    write_private(layout.sealed_dir / segment_name(1), b"source bytes")
    write_private(
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
    layout = store_layout(tmp_path)
    write_private(layout.sealed_dir / segment_name(1), b"source bytes")
    write_private(
        layout.sealed_dir / sealed_meta_name(1),
        b'{"record_count": -1, "salt_id": ""}',
    )

    entry = read_inventory(layout).sealed[0]

    assert entry.record_count is None, "an invalid count is withheld"
    assert not entry.has_metadata, "the entry does not claim metadata"


def test_inverted_sealed_metadata_is_not_trusted(tmp_path: Path) -> None:
    """A span that ends before it starts never decides an age eviction.

    The age planner deletes a sealed source from ``last_observed_at`` alone, so
    trusting an inverted span could remove a source still inside the window.
    """
    layout = store_layout(tmp_path)
    write_private(layout.sealed_dir / segment_name(1), b"source bytes")
    write_private(
        layout.sealed_dir / sealed_meta_name(1),
        json.dumps({
            "sequence": 1,
            "salt_id": "salt-1",
            "record_count": 3,
            "first_observed_at": "2026-11-19T00:10:00Z",
            "last_observed_at": "2026-11-19T00:00:00Z",
        }).encode(),
    )

    entry = read_inventory(layout).sealed[0]

    assert not entry.has_metadata, "the contradictory span is withheld"
    assert entry.last_observed_at is None, "no age decision can be made"


def test_a_sidecar_supplies_the_logical_size(tmp_path: Path) -> None:
    """The byte budget counts sidecar logical bytes, not XZ bytes."""
    layout = store_layout(tmp_path)
    write_private(layout.archive_dir / archive_name(1), b"x" * 40)
    write_sidecar(tmp_path, 1)

    inventory = read_inventory(layout)
    entry = inventory.archived[0]

    assert entry.physical_bytes == 40, "the archive's own size is measured"
    assert entry.logical_bytes == 100, "the sidecar's logical size is used"
    assert entry.record_count == 2, "the sidecar's count is reported"
    assert entry.sha256 == DIGEST, "the verified digest is reported"
    assert inventory.archived_logical_bytes == 100, "the budget sees logical bytes"


def test_a_sidecar_for_other_bytes_is_not_trusted(tmp_path: Path) -> None:
    """A sidecar whose recorded size disagrees with the archive is stale.

    A rewrite replaces the archive before its metadata. If the process stops
    between those steps the old sidecar still describes the previous bytes, and
    reporting its counts beside the new file would misstate the history.
    """
    layout = store_layout(tmp_path)
    write_private(layout.archive_dir / archive_name(1), b"x" * 40)
    write_sidecar(tmp_path, 1, physical_bytes=41)

    inventory = read_inventory(layout)

    assert inventory.corrupt_sidecars == (1,), "the stale sidecar is named"
    assert not inventory.archived[0].has_metadata, "its counts are withheld"
    assert inventory.archived[0].logical_bytes == 40, "physical bytes stand in"


def test_an_archive_without_a_sidecar_is_flagged(tmp_path: Path) -> None:
    """A missing sidecar is reported, not silently treated as zero bytes."""
    layout = store_layout(tmp_path)
    write_private(layout.archive_dir / archive_name(1), b"x" * 40)

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
    layout = store_layout(tmp_path)
    write_private(layout.archive_dir / archive_name(1), b"x" * 40)
    write_private(layout.archive_dir / sidecar_name(1), document)

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
        {
            "first_observed_at": "2026-11-19T00:00:00Z",
            "last_observed_at": "2026-11-18T23:59:00Z",
        },
    ],
    ids=[
        "short-digest",
        "non-hex",
        "negative",
        "no-moment",
        "no-salt",
        "bad-moment",
        "inverted-span",
    ],
)
def test_a_sidecar_field_must_hold_a_usable_value(
    tmp_path: Path, overrides: dict[str, object]
) -> None:
    """Every required field is validated before its value reaches a report.

    The inverted span is the dangerous one: the age planner deletes a trusted
    sidecar's archive from ``last_observed_at`` alone, so a span claiming the
    archive ends before it starts could remove rows still inside the window.
    """
    layout = store_layout(tmp_path)
    write_private(layout.archive_dir / archive_name(1), b"x" * 40)
    write_sidecar(tmp_path, 1, **overrides)

    assert read_inventory(layout).corrupt_sidecars == (1,), "the value is rejected"


def test_a_sidecar_whose_span_starts_and_ends_together_is_trusted(
    tmp_path: Path,
) -> None:
    """An equal span is the ordinary one-row archive, not an inverted one."""
    layout = store_layout(tmp_path)
    write_private(layout.archive_dir / archive_name(1), b"x" * 40)
    write_sidecar(
        tmp_path,
        1,
        first_observed_at="2026-11-19T00:00:00Z",
        last_observed_at="2026-11-19T00:00:00Z",
    )

    inventory = read_inventory(layout)

    assert not inventory.corrupt_sidecars, "one instant is a valid span"
    assert inventory.archived[0].has_metadata, "the sidecar is trusted"


def test_a_sidecar_behind_a_directory_is_corrupt(tmp_path: Path) -> None:
    """An unreadable sidecar path is damage, not absence."""
    layout = store_layout(tmp_path)
    write_private(layout.archive_dir / archive_name(1), b"x" * 40)
    (layout.archive_dir / sidecar_name(1)).mkdir()

    assert read_inventory(layout).corrupt_sidecars == (1,), "the read failure is closed"


def test_a_sidecar_symlink_is_corrupt(tmp_path: Path) -> None:
    """A link out of the archive directory is never followed.

    The link target is a schema-valid sidecar, so accepting the link would
    produce a trustworthy entry rather than a corruption flag; only refusing
    the link itself makes this test pass.
    """
    layout = store_layout(tmp_path)
    write_private(layout.archive_dir / archive_name(1), b"x" * 40)
    target = write_private(
        tmp_path / "elsewhere.json", json.dumps(sidecar_document(1)).encode()
    )
    (layout.archive_dir / sidecar_name(1)).symlink_to(target)

    assert read_inventory(layout).corrupt_sidecars == (1,), "the link is refused"


def test_an_oversized_sidecar_is_corrupt(tmp_path: Path) -> None:
    """A huge sidecar cannot be pulled into memory by an inventory scan.

    The document is otherwise valid and padded past the bound, so removing the
    size check would accept it rather than fail to parse it.
    """
    layout = store_layout(tmp_path)
    write_private(layout.archive_dir / archive_name(1), b"x" * 40)
    document = json.dumps(sidecar_document(1)).encode()
    padding = b" " * (64 * 1024 - len(document) + 1)
    write_private(layout.archive_dir / sidecar_name(1), document + padding)

    assert read_inventory(layout).corrupt_sidecars == (1,), "the file is refused unread"


def test_an_orphan_sidecar_is_flagged(tmp_path: Path) -> None:
    """A sidecar with no archive describes nothing and is reported."""
    layout = store_layout(tmp_path)
    write_sidecar(tmp_path, 2)

    inventory = read_inventory(layout)

    assert inventory.orphan_sidecars == (2,), "the orphan is named"
    assert not inventory.archived, "an orphan is not an archive"


def test_a_duplicate_prefers_the_sealed_source(tmp_path: Path) -> None:
    """A source that outlived a crash is the authority over its archive."""
    layout = store_layout(tmp_path)
    write_private(layout.sealed_dir / segment_name(1), b"source bytes")
    write_private(layout.archive_dir / archive_name(1), b"x" * 40)
    write_sidecar(tmp_path, 1)

    inventory = read_inventory(layout)

    assert len(inventory.archived) == 1, "the archive is still inventoried"
    assert [entry.state for entry in inventory.effective] == ["sealed"], (
        "one sequence is read once, from the source"
    )
    assert inventory.highest_sequence == 1, "the duplicate is one sequence"


def test_gaps_start_at_the_retention_floor(tmp_path: Path) -> None:
    """Sequences below the floor were evicted on purpose, not lost."""
    layout = store_layout(tmp_path)
    for sequence in (1, 4):
        write_private(layout.sealed_dir / segment_name(sequence), b"rows")

    assert read_inventory(layout).gaps == (2, 3), "the missing middle is named"
    assert read_inventory(layout, retention_floor=3).gaps == (3,), (
        "an intentionally evicted prefix is not a gap"
    )
    assert not read_inventory(layout, retention_floor=5).gaps, (
        "a floor above every sequence leaves nothing missing"
    )


def test_gap_reporting_is_bounded(tmp_path: Path) -> None:
    """A sparse sequence cannot make one report enumerate forever.

    The two segments are exactly one more gap apart than the bound, so the
    list is cut short and the flag says so.
    """
    layout = store_layout(tmp_path)
    for sequence in (1, MAX_GAP_SEQUENCES + 3):
        write_private(layout.sealed_dir / segment_name(sequence), b"rows")

    inventory = read_inventory(layout)

    assert len(inventory.gaps) == MAX_GAP_SEQUENCES, "the list stops at the bound"
    assert inventory.gaps_truncated, "the report says more sequences are missing"


def test_a_gap_list_at_the_bound_is_not_truncated(tmp_path: Path) -> None:
    """Exactly the bound of missing sequences is reported in full."""
    layout = store_layout(tmp_path)
    for sequence in (1, MAX_GAP_SEQUENCES + 2):
        write_private(layout.sealed_dir / segment_name(sequence), b"rows")

    inventory = read_inventory(layout)

    assert len(inventory.gaps) == MAX_GAP_SEQUENCES, "every gap is listed"
    assert not inventory.gaps_truncated, "nothing is withheld at the bound"


def test_inventory_byte_totals_follow_state(tmp_path: Path) -> None:
    """Sealed sources count physically; archives count logically."""
    layout = store_layout(tmp_path)
    write_private(layout.sealed_dir / segment_name(1), b"s" * 30)
    write_private(layout.archive_dir / archive_name(2), b"x" * 40)
    write_sidecar(tmp_path, 2)

    inventory = read_inventory(layout)

    assert inventory.sealed_bytes == 30, "sealed bytes are the source's size"
    assert inventory.archived_logical_bytes == 100, "archived bytes are logical"
    assert inventory.highest_sequence == 2, "the highest closed sequence is known"


def test_sealed_entries_carry_their_path_and_state(tmp_path: Path) -> None:
    """An entry names exactly the file and state that own it."""
    layout = store_layout(tmp_path)
    path = write_private(layout.sealed_dir / segment_name(1), b"rows")

    entry = read_inventory(layout).sealed[0]

    assert entry == SegmentEntry(
        sequence=1, path=path, state="sealed", physical_bytes=4, logical_bytes=4
    ), "the entry describes the source without reading it"


def test_a_symlinked_sealed_directory_is_refused(tmp_path: Path) -> None:
    """Inventory never follows a link out of the store.

    A retention pass deletes what the inventory names, so a sealed directory
    that is a link would present a file outside the store as expired history.
    """
    layout = store_layout(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    write_private(outside / segment_name(1), b"external bytes")
    layout.sealed_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafePathError):
        read_inventory(layout)


def test_a_symlinked_archive_directory_is_refused(tmp_path: Path) -> None:
    """An archive directory outside the store is not inventoried either."""
    layout = store_layout(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    write_private(outside / archive_name(1), b"external archive")
    layout.archive_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafePathError):
        read_inventory(layout)


def test_a_symlinked_parent_of_the_store_is_refused(tmp_path: Path) -> None:
    """Every component is checked, not only the directory being listed.

    A link above the sealed directory redirects the listing just as surely as
    one at the final component, and the final-component no-follow flag cannot
    see it.
    """
    outside = tmp_path / "outside"
    (outside / "store").mkdir(parents=True)
    write_private(outside / "store" / segment_name(1), b"external bytes")
    (tmp_path / "link").symlink_to(outside / "store", target_is_directory=True)

    with pytest.raises(UnsafePathError):
        read_inventory(store_layout(tmp_path / "link"))
