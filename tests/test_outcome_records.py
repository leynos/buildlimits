"""Bounded JSON Lines records and the pure cache-outcome compactor."""

import dataclasses
import datetime as dt
import json
import typing as typ
from types import ModuleType

import pytest
from outcome_test_helpers import outcome_module

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

_NOW = dt.datetime(2026, 10, 24, 12, tzinfo=dt.UTC)


def _fixture() -> tuple[ModuleType, typ.Any, tuple[str, ...]]:
    """Build stable header, request record and H3 window snapshot lines."""
    key_module = outcome_module("outcome_key")
    records = outcome_module("outcome_records")
    salt = key_module.Salt(key=b"s" * 32, salt_id="fixed-salt")
    identity = key_module.ToolchainIdentity(
        compiler_digest="a" * 64,
        rustup_toolchain_digest="b" * 64,
        cache_version="6",
        server_version="0.14.0",
    )
    cache_key = key_module.CacheKey(
        crate_name="serde",
        crate_origin="registry:serde@1.2.3",
        target="host",
        argument_digest="c" * 64,
        toolchain_identity=identity,
    )
    header = records.StoreHeader(salt_id=salt.salt_id, written_at=_NOW)
    outcome = records.OutcomeRecord(
        key=cache_key,
        outcome="hit",
        origin="registry",
        runtime_seconds=0.25,
        observed_at=_NOW,
        parser_class="cacheable",
        child_sighting="seen",
        child_samples=3,
    )
    summary = records.WindowSummary(
        observed_at=_NOW,
        server_version="0.14.0",
        window_groups=1,
        requests_not_compile=0,
        requests_not_cacheable=0,
        predicted_not_compile=0,
        predicted_declined=0,
        predicted_cacheable=1,
    )
    lines = tuple(records.encode_line(value) for value in (header, outcome, summary))
    return records, header, lines


def test_header_outcome_and_window_summary_snapshot(
    snapshot: SnapshotAssertion,
) -> None:
    """Pin one stable header, request record and H3 summary line."""
    _, _, lines = _fixture()

    assert lines == snapshot, "the store's bounded JSON Lines shape is stable"


def test_encoded_lines_round_trip_to_typed_values() -> None:
    """Decode the snapshot-shaped document without changing its semantic rows."""
    records, header, lines = _fixture()
    decoded = records.decode_lines("\n".join(lines) + "\n")

    assert decoded.header == header, "header values survive encoding"
    assert len(decoded.records) == 2, "request and summary records survive encoding"
    assert records.encode_line(decoded.records[0]) == lines[1], (
        "the request record round-trips semantically"
    )
    assert records.encode_line(decoded.records[1]) == lines[2], (
        "the H3 summary round-trips semantically"
    )


def test_decoder_drops_only_a_torn_final_line() -> None:
    """A partial append is ignored while an earlier complete record survives."""
    records, _, lines = _fixture()
    document = "\n".join(lines[:2]) + '\n{"kind":"outcome"'

    decoded = records.decode_lines(document)

    assert len(decoded.records) == 1, "the incomplete tail is dropped"
    assert decoded.records[0].outcome == "hit", "the complete row remains"


@pytest.mark.parametrize(
    "document",
    ["", "[]\n", '{"version":2}\n', '{"format":"wrong"}\n'],
    ids=("empty", "wrong-shape", "partial-header", "wrong-format"),
)
def test_malformed_or_incompatible_documents_fail_closed(document: str) -> None:
    """Malformed headers and unsupported versions never become empty history."""
    records = outcome_module("outcome_records")

    result = records.decode_lines(document)

    assert isinstance(result, records.RecordFailure), (
        "malformed or incompatible documents fail closed"
    )
    assert result.reason in {"corrupt", "version_mismatch"}, (
        "the failure distinguishes corrupt data from version mismatch"
    )


def test_unknown_bounded_vocabulary_is_rejected_during_decode() -> None:
    """A stored parser class outside the closed vocabulary is corrupt data."""
    records, _, lines = _fixture()
    raw = json.loads(lines[1])
    raw["parser_class"] = "cache-hit"
    document = lines[0] + "\n" + json.dumps(raw, separators=(",", ":")) + "\n"

    result = records.decode_lines(document)

    assert isinstance(result, records.RecordFailure), (
        "unknown parser vocabulary fails closed"
    )
    assert result.reason == "corrupt", "unknown parser vocabulary is corrupt data"


def test_compactor_expires_old_rows_and_skips_ageing_on_clock_skew() -> None:
    """Compaction uses supplied time values and reports the clock-skew branch."""
    records, header, lines = _fixture()
    decoded = records.decode_lines("\n".join(lines) + "\n")
    outcome = decoded.records[0]
    old = dataclasses.replace(
        outcome, observed_at=dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
    )
    decoded = dataclasses.replace(decoded, records=(old, decoded.records[1]))

    aged = records.compact(decoded, now=_NOW)
    skewed = records.compact(decoded, now=_NOW, clock_skew=True)

    assert aged.records == (decoded.records[1],), "old outcome rows expire"
    assert aged.discarded == 1, "the expiry is counted"
    assert skewed.records == decoded.records, "clock skew skips age-based deletion"
    assert skewed.store_reason == "clock_skew", "the skipped ageing reason is explicit"
    assert header.salt_id == skewed.header.salt_id, "compaction keeps salt identity"


def test_compactor_applies_record_count_and_byte_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compaction evicts oldest entries until both configured bounds hold."""
    records, _, lines = _fixture()
    decoded = records.decode_lines("\n".join(lines) + "\n")
    monkeypatch.setattr(records, "MAX_COMPACT_RECORDS", 1)
    monkeypatch.setattr(records, "MAX_COMPACT_BYTES", 512)

    compacted = records.compact(decoded, now=_NOW)

    assert len(compacted.records) <= 1, "the record-count cap is enforced"
    encoded_size = sum(
        len(records.encode_line(record).encode()) + 1
        for record in (compacted.header, *compacted.records)
    )
    assert encoded_size <= 512, "the serialized byte cap is enforced"
