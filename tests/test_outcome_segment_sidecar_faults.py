"""The sidecar integrity boundary of the read-only inventory.

An archive's sidecar is the planner's evidence for its logical size and its
observed span, so a sidecar that is missing, stale, inverted or unreadable is
reported as a fault instead of being trusted. These tests pin each way a
sidecar can fail, because the age planner deletes archives from the span it
names.
"""

import collections.abc as cabc
import json
import typing as typ
from pathlib import Path

import pytest
from segment_test_helpers import (
    DIGEST,
    sidecar_document,
    store_layout,
    write_private,
    write_sidecar,
)

from buildlimits.outcome_segment_inventory import Inventory, read_inventory
from buildlimits.outcome_segments import archive_name, sidecar_name

if typ.TYPE_CHECKING:
    from buildlimits.outcome_segments import SegmentLayout


def _inventory_after(
    tmp_path: Path, prepare: cabc.Callable[[SegmentLayout], object]
) -> Inventory:
    """Publish one archive, prepare its sidecar, and read the inventory.

    Returns
    -------
    Inventory
        The inventory the planner would see after ``prepare`` ran against the
        archive directory.
    """
    layout = store_layout(tmp_path)
    write_private(layout.archive_dir / archive_name(1), b"x" * 40)
    prepare(layout)
    return read_inventory(layout)


def test_a_sidecar_supplies_the_logical_size(tmp_path: Path) -> None:
    """The byte budget counts sidecar logical bytes, not XZ bytes."""
    inventory = _inventory_after(tmp_path, lambda _layout: write_sidecar(tmp_path, 1))
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
    inventory = _inventory_after(
        tmp_path, lambda _layout: write_sidecar(tmp_path, 1, physical_bytes=41)
    )

    assert inventory.corrupt_sidecars == (1,), "the stale sidecar is named"
    assert not inventory.archived[0].has_metadata, "its counts are withheld"
    assert inventory.archived[0].logical_bytes == 40, "physical bytes stand in"


def test_an_archive_without_a_sidecar_is_flagged(tmp_path: Path) -> None:
    """A missing sidecar is reported, not silently treated as zero bytes."""
    inventory = _inventory_after(tmp_path, lambda _layout: None)

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

    def write_document(layout: SegmentLayout) -> None:
        """Write the parametrized document as the archive's sidecar."""
        write_private(layout.archive_dir / sidecar_name(1), document)

    inventory = _inventory_after(tmp_path, write_document)

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
    inventory = _inventory_after(
        tmp_path, lambda _layout: write_sidecar(tmp_path, 1, **overrides)
    )

    assert inventory.corrupt_sidecars == (1,), "the value is rejected"


def test_a_sidecar_whose_span_starts_and_ends_together_is_trusted(
    tmp_path: Path,
) -> None:
    """An equal span is the ordinary one-row archive, not an inverted one."""
    inventory = _inventory_after(
        tmp_path,
        lambda _layout: write_sidecar(
            tmp_path,
            1,
            first_observed_at="2026-11-19T00:00:00Z",
            last_observed_at="2026-11-19T00:00:00Z",
        ),
    )

    assert not inventory.corrupt_sidecars, "one instant is a valid span"
    assert inventory.archived[0].has_metadata, "the sidecar is trusted"


def test_a_sidecar_behind_a_directory_is_corrupt(tmp_path: Path) -> None:
    """An unreadable sidecar path is damage, not absence."""
    inventory = _inventory_after(
        tmp_path, lambda layout: (layout.archive_dir / sidecar_name(1)).mkdir()
    )

    assert inventory.corrupt_sidecars == (1,), "the read failure is closed"


def test_a_sidecar_symlink_is_corrupt(tmp_path: Path) -> None:
    """A link out of the archive directory is never followed.

    The link target is a schema-valid sidecar, so accepting the link would
    produce a trustworthy entry rather than a corruption flag; only refusing
    the link itself makes this test pass.
    """

    def link_elsewhere(layout: SegmentLayout) -> None:
        """Symlink the sidecar to a valid document outside the archive."""
        target = write_private(
            tmp_path / "elsewhere.json", json.dumps(sidecar_document(1)).encode()
        )
        (layout.archive_dir / sidecar_name(1)).symlink_to(target)

    inventory = _inventory_after(tmp_path, link_elsewhere)

    assert inventory.corrupt_sidecars == (1,), "the link is refused"


def test_an_oversized_sidecar_is_corrupt(tmp_path: Path) -> None:
    """A huge sidecar cannot be pulled into memory by an inventory scan.

    The document is otherwise valid and padded past the bound, so removing the
    size check would accept it rather than fail to parse it.
    """

    def pad_sidecar(layout: SegmentLayout) -> None:
        """Write a valid sidecar padded past the reader's size bound."""
        document = json.dumps(sidecar_document(1)).encode()
        padding = b" " * (64 * 1024 - len(document) + 1)
        write_private(layout.archive_dir / sidecar_name(1), document + padding)

    inventory = _inventory_after(tmp_path, pad_sidecar)

    assert inventory.corrupt_sidecars == (1,), "the file is refused unread"


def test_an_orphan_sidecar_is_flagged(tmp_path: Path) -> None:
    """A sidecar with no archive describes nothing and is reported."""
    layout = store_layout(tmp_path)
    write_sidecar(tmp_path, 2)

    inventory = read_inventory(layout)

    assert inventory.orphan_sidecars == (2,), "the orphan is named"
    assert not inventory.archived, "an orphan is not an archive"
