"""Archive scanning, compression and prefix-trim boundaries.

A published archive is trusted only after it decompresses to the facts the
source described. These tests pin the facts, every header and row fault, and
the exact boundary behaviour of the three prefix trims the planner can ask
for, including the row observed exactly at the age cutoff.
"""

import datetime as dt
import hashlib
import io
import json
import lzma

import pytest
from outcome_test_helpers import attributed_record
from segment_test_helpers import document as encode_document

from buildlimits import outcome_record_encode
from buildlimits.outcome_archive_trim import Trim, policy_dropped_bytes
from buildlimits.outcome_archive_verify import (
    ArchiveFacts,
    SegmentFaultError,
    scan_archive,
    write_archive,
)
from buildlimits.outcome_record_format import StoreHeader, StoreRecord
from buildlimits.outcome_record_stream import LineStream

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)


def _header(salt_id: str = "salt-1") -> StoreHeader:
    """Build one supported store header.

    Returns
    -------
    StoreHeader
        A version-1 header naming ``salt_id``.
    """
    return StoreHeader(salt_id, NOW)


def _records(count: int) -> tuple[StoreRecord, ...]:
    """Build ``count`` attributed records, one minute apart and increasing.

    Returns
    -------
    tuple[object, ...]
        The records, oldest first.
    """
    return tuple(
        attributed_record(observed_at=NOW + dt.timedelta(minutes=index))
        for index in range(count)
    )


def _row(document: bytes, index: int) -> bytes:
    """Return one newline-terminated row of ``document``.

    Returns
    -------
    bytes
        The row including its trailing newline.
    """
    return document.splitlines(keepends=True)[index]


def _scan(document: bytes) -> ArchiveFacts:
    """Scan one plain document to its end.

    Returns
    -------
    ArchiveFacts
        The facts the document holds.
    """
    return scan_archive(LineStream(io.BytesIO(document)))


def _write(document: bytes, trim: Trim) -> tuple[ArchiveFacts, bytes]:
    """Compress ``document`` with one trim and decompress the result.

    Returns
    -------
    tuple[ArchiveFacts, bytes]
        The facts reported for the archive and its decompressed bytes.
    """
    sink = io.BytesIO()
    facts = write_archive(LineStream(io.BytesIO(document)), sink, preset=0, trim=trim)
    return facts, lzma.decompress(sink.getvalue())


def test_facts_describe_the_document_actually_read() -> None:
    """Count, logical size, digest and observation span all come from rows."""
    document = encode_document(_records(3))

    facts = _scan(document)

    assert facts.salt_id == "salt-1", "the header supplies the domain"
    assert facts.record_count == 3, "the header is not a record"
    assert facts.logical_bytes == len(document), "newlines and header are counted"
    assert facts.sha256 == hashlib.sha256(document).hexdigest(), "the digest is exact"
    assert facts.first_observed_at == NOW, "the earliest observation is reported"
    assert facts.last_observed_at == NOW + dt.timedelta(minutes=2), (
        "the latest observation is reported"
    )
    assert not facts.had_torn_tail, "a complete document has no torn tail"


def test_a_header_only_document_has_no_observation_span() -> None:
    """An empty segment is valid history with no rows to date."""
    facts = _scan(encode_document(()))

    assert facts.record_count == 0, "there are no records"
    assert facts.first_observed_at is None, "there is no first moment"
    assert facts.last_observed_at is None, "there is no last moment"


def test_an_unterminated_header_is_tolerated_as_a_torn_tail() -> None:
    """A header written without its newline still names a readable document."""
    document = outcome_record_encode.encode_line(_header()).encode()

    facts = _scan(document)

    assert facts.salt_id == "salt-1", "the header decodes"
    assert facts.record_count == 0, "there are no rows"
    assert facts.had_torn_tail, "the missing newline is reported"


def test_a_version_the_store_does_not_support_is_named() -> None:
    """A future schema is a distinct fault, not generic corruption."""
    document = (
        json.dumps({
            "format": "buildlimits-cache-outcomes",
            "version": 2,
            "salt_id": "salt-1",
            "written_at": "2026-11-19T00:00:00Z",
        })
        + "\n"
    ).encode()

    with pytest.raises(SegmentFaultError) as raised:
        _scan(document)

    assert raised.value.reason == "version_mismatch", "the version fault is closed"


@pytest.mark.parametrize(
    "document",
    [b"", b"not json\n", b'{"kind": "outcome"}\n'],
    ids=["empty", "unparsable", "record-first"],
)
def test_a_document_without_a_header_is_corrupt(document: bytes) -> None:
    """No record may be read from a file whose first row is not a header."""
    with pytest.raises(SegmentFaultError) as raised:
        _scan(document)

    assert raised.value.reason == "corrupt", "the fault is closed"


def test_a_terminated_invalid_row_is_corrupt() -> None:
    """A complete row that does not decode cannot be dropped as a torn tail."""
    document = encode_document(_records(1)) + b'{"kind": "unknown"}\n'

    with pytest.raises(SegmentFaultError) as raised:
        _scan(document)

    assert raised.value.reason == "corrupt", "a terminated row must decode"


def test_an_unterminated_invalid_tail_is_dropped() -> None:
    """A crash-truncated final row is not history and is reported as torn."""
    document = encode_document(_records(2)) + b'{"kind": "outc'

    facts = _scan(document)

    assert facts.record_count == 2, "only complete rows are represented"
    assert facts.had_torn_tail, "the dropped fragment is reported"


def test_a_truncated_archive_is_a_fault() -> None:
    """A compressed source shorter than its declared stream is unusable."""
    archive = lzma.compress(encode_document(_records(1)), format=lzma.FORMAT_XZ)

    with pytest.raises(SegmentFaultError) as raised:
        scan_archive(LineStream(io.BytesIO(archive[:-8]), is_compressed=True))

    assert raised.value.reason == "truncated_xz", "the compression fault is closed"


def test_a_complete_archive_scans_like_its_plain_source() -> None:
    """Compression preserves every fact the source proved."""
    document = encode_document(_records(2))
    archive = lzma.compress(document, format=lzma.FORMAT_XZ)

    scanned = scan_archive(LineStream(io.BytesIO(archive), is_compressed=True))

    assert scanned.matches(_scan(document)), "count, size and digest all agree"


def test_an_empty_trim_rewrites_the_document_byte_for_byte() -> None:
    """The default trim preserves the source exactly."""
    document = encode_document(_records(3))

    facts, archived = _write(document, Trim())

    assert Trim().is_empty, "an empty trim is inert"
    assert archived == document, "no row is dropped or reordered"
    assert facts.logical_bytes == len(document), "the written size is reported"
    assert facts.sha256 == hashlib.sha256(document).hexdigest(), (
        "the written digest is exact"
    )
    assert facts.matches(_scan(document)), "the archive proves the source"


def test_a_record_trim_drops_exactly_the_leading_rows() -> None:
    """A count trim removes a prefix and keeps document order."""
    document = encode_document(_records(3))

    facts, archived = _write(document, Trim(records=1))

    assert archived.splitlines(keepends=True)[:2] == [
        _row(document, 0),
        _row(document, 2),
    ], "the first record is gone and the header stays first"
    assert facts.record_count == 2, "the count follows the written rows"


def test_a_count_equal_to_every_record_keeps_only_the_header() -> None:
    """A trim that drops all records still writes a valid, empty document."""
    document = encode_document(_records(2))

    facts, archived = _write(document, Trim(records=2))

    assert archived == _row(document, 0), "only the header remains"
    assert facts.record_count == 0, "the empty archive has no records"


def test_an_age_trim_keeps_the_record_at_the_cutoff() -> None:
    """A row observed exactly at the cutoff is retained; older rows are not."""
    document = encode_document(_records(3))
    cutoff = NOW + dt.timedelta(minutes=1)

    facts, archived = _write(document, Trim(before=cutoff))

    assert _row(document, 2) in archived, "the row at the cutoff survives"
    assert _row(document, 1) not in archived, "the older row is dropped"
    assert _row(document, 3) in archived, "the newer row survives"
    assert facts.record_count == 2, "the count excludes the dropped row"
    assert facts.first_observed_at == cutoff, "the span starts at the cutoff"


def test_a_byte_trim_keeps_a_suffix_within_its_allowance() -> None:
    """The allowed size is the whole written document, header included."""
    document = encode_document(_records(3))
    header_bytes = len(_row(document, 0))
    last_row_bytes = len(_row(document, 3))

    facts, archived = _write(
        document,
        Trim(allowed_bytes=header_bytes + last_row_bytes, source_bytes=len(document)),
    )

    assert archived == _row(document, 0) + _row(document, 3), (
        "the largest suffix that fits the allowance is kept"
    )
    assert facts.logical_bytes == header_bytes + last_row_bytes, (
        "the allowance is measured in the bytes the archive actually holds"
    )


def test_a_later_expired_row_does_not_cost_an_earlier_eligible_row() -> None:
    """The byte allowance counts every policy drop, wherever it falls.

    The document is ordered *at cutoff, expired, fresh*. Both eligible rows
    fit the allowance once the expired row leaves, so a decision that measured
    only the rows dropped before the current one would discard the cutoff row
    while still counting the expired row's bytes against the allowance.
    """
    at_cutoff = attributed_record(observed_at=NOW + dt.timedelta(minutes=1))
    expired = attributed_record(observed_at=NOW)
    fresh = attributed_record(observed_at=NOW + dt.timedelta(minutes=2))
    document = encode_document((at_cutoff, expired, fresh))
    cutoff = NOW + dt.timedelta(minutes=1)
    trim = Trim(before=cutoff)
    expired_row = _row(document, 2)

    policy_dropped = policy_dropped_bytes(LineStream(io.BytesIO(document)), trim)
    allowance = len(document) - policy_dropped
    facts, archived = _write(
        document,
        Trim(
            before=cutoff,
            allowed_bytes=allowance,
            source_bytes=len(document),
            policy_bytes=policy_dropped,
        ),
    )

    assert policy_dropped == len(expired_row), "only the expired row is measured"
    assert _row(document, 1) in archived, "the row at the cutoff survives"
    assert expired_row not in archived, "the expired row is still dropped"
    assert _row(document, 3) in archived, "the fresh row survives"
    assert archived == _row(document, 0) + _row(document, 1) + _row(document, 3), (
        "the written document is exactly the two eligible rows and the header"
    )
    assert facts.logical_bytes == allowance, "the document meets the allowance"


def test_one_byte_below_the_document_size_drops_the_whole_suffix() -> None:
    """The boundary is inclusive: an allowance one byte short keeps nothing."""
    document = encode_document(_records(2))
    header_bytes = len(_row(document, 0))
    last_row_bytes = len(_row(document, 2))

    facts, archived = _write(
        document,
        Trim(
            allowed_bytes=header_bytes + last_row_bytes - 1,
            source_bytes=len(document),
        ),
    )

    assert archived == _row(document, 0), "no record fits the allowance"
    assert facts.record_count == 0, "the archive carries the header alone"


def test_an_unterminated_tail_is_not_written_into_the_archive() -> None:
    """A torn final fragment never becomes a record in a published archive."""
    document = encode_document(_records(1)) + b'{"kind": "outc'

    facts, archived = _write(document, Trim())

    assert archived == encode_document(_records(1)), (
        "the archive holds only complete rows"
    )
    assert facts.had_torn_tail, "the source's tear is reported on the archive"


def test_fact_matching_ignores_the_observation_span() -> None:
    """A rewrite that changes only dates still proves the same content."""
    document = encode_document(_records(2))
    facts = _scan(document)
    shifted = ArchiveFacts(
        salt_id=facts.salt_id,
        record_count=facts.record_count,
        logical_bytes=facts.logical_bytes,
        sha256=facts.sha256,
        first_observed_at=None,
        last_observed_at=None,
        had_torn_tail=facts.had_torn_tail,
    )

    assert facts.matches(shifted), "count, size and digest are the contract"
    assert not facts.matches(
        ArchiveFacts(
            salt_id=facts.salt_id,
            record_count=facts.record_count + 1,
            logical_bytes=facts.logical_bytes,
            sha256=facts.sha256,
            first_observed_at=None,
            last_observed_at=None,
            had_torn_tail=facts.had_torn_tail,
        )
    ), "a different count never matches"
