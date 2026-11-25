"""Archival and retention preserve timing, correlation and record order.

Archiving rewrites a segment as XZ by copying each row's original bytes, so a
row's timing evidence must survive the round trip verbatim. These tests pin
that for both shapes the store can hold: a version 3 row with the complete
timing group, and a version 2 row from before availability was recorded. The
second must gain nothing: no availability time, no attempt, no generation.

They also pin that the archive never re-sorts rows by their timestamps. A
verified row can be appended after a row it precedes in time, because rows are
written in decision order rather than acceptance order.
"""

import datetime as dt
import io
import json
import lzma
import typing as typ
from pathlib import Path

import pytest
from outcome_test_helpers import cache_key
from outcome_version_helpers import VERSION_TWO, older_document_row
from segment_test_helpers import document

from buildlimits.outcome_archive_encode import read_archive_facts
from buildlimits.outcome_archive_trim import Trim
from buildlimits.outcome_archive_verify import write_archive
from buildlimits.outcome_record_codec import decode_lines
from buildlimits.outcome_record_format import (
    DecodedStore,
    OutcomeRecord,
    StoreHeader,
)
from buildlimits.outcome_record_stream import LineStream

NOW = dt.datetime(2026, 11, 24, 12, tzinfo=dt.UTC)
ACCEPTED = NOW + dt.timedelta(seconds=30)
COMPLETED = NOW + dt.timedelta(seconds=10)


def _verified(index: int, *, accepted: dt.datetime = ACCEPTED) -> OutcomeRecord:
    """Return one version 5 row with its complete correlation group."""
    return OutcomeRecord(
        key=cache_key(),
        outcome="hit",
        origin="registry",
        runtime_seconds=1.0,
        observed_at=accepted,
        parser_class="cacheable",
        child_sighting="unavailable",
        child_samples=0,
        outcome_reason="none",
        outcome_attribution="solo",
        admitted_at=NOW,
        label_time_provenance="verified",
        completed_at=COMPLETED,
        attempt=f"g{index}",
        supervisor_generation="generation-a",
    )


def _legacy(index: int) -> OutcomeRecord:
    """Return one version 2 row from before availability was recorded.

    The encoder writes this shape without the timing group, which is exactly
    what an older release produced.
    """
    return OutcomeRecord(
        key=cache_key(),
        outcome="miss",
        origin="registry",
        runtime_seconds=2.0,
        observed_at=NOW + dt.timedelta(seconds=index),
        parser_class="cacheable",
        child_sighting="unavailable",
        child_samples=0,
        outcome_reason="none",
        outcome_attribution="solo",
        admitted_at=NOW - dt.timedelta(seconds=5),
    )


def _archive_bytes(rows: tuple[OutcomeRecord, ...], *, preset: int = 0) -> bytes:
    """Return the archive bytes for ``rows``.

    Returns
    -------
    bytes
        The XZ archive ``write_archive`` produced.
    """
    sink = io.BytesIO()
    write_archive(
        LineStream(io.BytesIO(document(rows))), sink, preset=preset, trim=Trim()
    )
    return sink.getvalue()


def _round_trip(rows: tuple[OutcomeRecord, ...]) -> DecodedStore:
    """Archive and decompress ``rows``, then decode the result.

    Returns
    -------
    DecodedStore
        The decoded archive contents.
    """
    decompressed = lzma.decompress(_archive_bytes(rows))
    decoded = decode_lines(decompressed.decode("utf-8"))
    assert isinstance(decoded, DecodedStore), "the archive decodes"
    return decoded


def test_a_verified_row_round_trips_through_archival_unchanged() -> None:
    """Every timing and correlation field survives compression verbatim."""
    rows = (_verified(1),)

    decoded = _round_trip(rows)

    (restored,) = decoded.records
    assert isinstance(restored, OutcomeRecord), "the row is an outcome"
    assert restored == rows[0], "every field survives the archive exactly"
    assert restored.observed_at == ACCEPTED, "the acceptance time is preserved"
    assert restored.label_time_provenance == "verified", "the proof survives"
    assert restored.completed_at == COMPLETED, "the completion time stays distinct"
    assert restored.attempt == "g1", "the attempt identity is preserved"
    assert restored.supervisor_generation == "generation-a", "so is the generation"


def test_a_legacy_row_gains_no_fabricated_timing() -> None:
    """A version 2 row archives without acquiring evidence it never had."""
    rows = (_legacy(1),)

    decoded = _round_trip(rows)

    (restored,) = decoded.records
    assert isinstance(restored, OutcomeRecord), "the row is an outcome"
    assert restored.completed_at is None, "no completion time is invented"
    assert restored.label_time_provenance is None, "no label time is proven"
    assert restored.attempt is None, "no attempt identity is invented"
    assert restored.supervisor_generation is None, "no generation is invented"
    assert restored.admitted_at == NOW - dt.timedelta(seconds=5), (
        "the version 2 admission time is preserved"
    )


def test_archival_keeps_append_order_rather_than_sorting_by_time() -> None:
    """A row appended after an earlier one is not reordered by its timestamps.

    The first row is accepted later than the second, which is physically
    possible when a slow compilation outlives a fast one. Sorting by any
    timestamp would claim an order the writer never produced.
    """
    late_acceptance = _verified(1, accepted=ACCEPTED + dt.timedelta(minutes=5))
    early_acceptance = _verified(2, accepted=ACCEPTED)

    decoded = _round_trip((late_acceptance, early_acceptance))

    first, second = typ.cast("tuple[OutcomeRecord, ...]", decoded.records)
    assert first.attempt == "g1", "the first appended row is still first"
    assert second.attempt == "g2", "the second appended row is still second"
    assert first.observed_at > second.observed_at, (
        "their acceptance times are genuinely out of order"
    )


def test_archive_facts_use_the_observation_span_not_the_acceptance_span(
    tmp_path: Path,
) -> None:
    """Retention measures the label moments the rows recorded."""
    rows = (_verified(1), _legacy(1))
    path = tmp_path / "segment.jsonl.xz"
    path.write_bytes(_archive_bytes(rows))

    facts = read_archive_facts(path)

    assert facts.record_count == 2, "both rows are archived"
    assert facts.first_observed_at == NOW + dt.timedelta(seconds=1), (
        "the earliest historical observation is named"
    )
    assert facts.last_observed_at == ACCEPTED, (
        "the latest label moment is named, which is the reading that accepted it"
    )


@pytest.mark.parametrize("preset", [0, 1])
def test_compression_preserves_the_document_bytes_exactly(preset: int) -> None:
    """Archival copies original row bytes, so a decoder sees the same document."""
    rows = (_verified(1), _legacy(1))

    assert lzma.decompress(_archive_bytes(rows, preset=preset)) == document(rows), (
        "the archived document is byte-identical to its source"
    )


def test_a_version_two_header_still_decodes_the_rows_it_holds() -> None:
    """A version 2 document reads, and its rows state no verified availability."""
    rows = (_legacy(1),)
    body = json.dumps(
        older_document_row(rows[0], version=VERSION_TWO), separators=(",", ":")
    )
    header = StoreHeader("salt-1", NOW)

    legacy_header = json.dumps({
        "format": "buildlimits-cache-outcomes",
        "version": 2,
        "salt_id": header.salt_id,
        "written_at": "2026-11-24T12:00:00Z",
    })

    decoded = decode_lines(f"{legacy_header}\n{body}\n")

    assert isinstance(decoded, DecodedStore), "the version 2 document decodes"
    (restored,) = decoded.records
    assert isinstance(restored, OutcomeRecord), "the row is an outcome"
    assert restored.label_time_provenance is None, "it cannot prove its label time"
