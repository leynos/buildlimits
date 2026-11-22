"""Segment naming, layout derivation and sidecar schema stability.

These names are the contract shared by the writer, the archiver and every
report, so the tests pin their shape rather than any implementation detail.
"""

import json
from pathlib import Path

from segment_test_helpers import sidecar_document

from buildlimits.outcome_segment_sidecar import SIDECAR_SCHEMA
from buildlimits.outcome_segments import (
    ARCHIVE_DIR_NAME,
    SegmentLayout,
    archive_name,
    sealed_meta_name,
    segment_name,
    sidecar_name,
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


def test_sidecar_json_is_closed_and_stable() -> None:
    """The sidecar schema names every field a later run depends on."""
    document = sidecar_document(3)

    assert document["schema"] == SIDECAR_SCHEMA, "the schema number is carried"
    assert document["sequence"] == 3, "the sequence is carried"
    assert document["format_version"] == 1, "the record schema is preserved"
    assert document["xz_check"] == "crc64", "the integrity check is named"
    assert json.loads(json.dumps(document, sort_keys=True)) == document, (
        "the sidecar is plain JSON"
    )
