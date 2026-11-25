"""The public store path refuses every malformed or path-bearing cache key.

A key is the one part of a store row that names a compilation, so it is also
the part that could carry a local path. The checks live in
:mod:`buildlimits.outcome_record_key`, but a test that only called them
directly would still pass if the encoder stopped applying them. These cases
therefore drive :func:`encode_line` and :func:`decode_record_line`, so removing
or weakening the validation fails here rather than only in review.

Each refusal is paired with the valid control, so a change that simply refuses
everything cannot pass the suite either.
"""

import dataclasses as dc
import datetime as dt
import json
import typing as typ

import pytest

from buildlimits.outcome_key import CacheKey, ToolchainIdentity
from buildlimits.outcome_record_codec import decode_record_line
from buildlimits.outcome_record_encode import encode_line
from buildlimits.outcome_record_format import MAX_RECORD_BYTES, OutcomeRecord

NOW = dt.datetime(2026, 11, 25, 12, tzinfo=dt.UTC)
_VALID_DIGEST = "a" * 64


def _identity(
    *,
    compiler_digest: str = _VALID_DIGEST,
    rustup_toolchain_digest: str = _VALID_DIGEST,
    cache_version: str = "6",
    server_version: str = "0.14.0",
) -> ToolchainIdentity:
    """Return a valid toolchain identity with the named fields replaced.

    Returns
    -------
    ToolchainIdentity
        The identity a case is built from.
    """
    return ToolchainIdentity(
        compiler_digest=compiler_digest,
        rustup_toolchain_digest=rustup_toolchain_digest,
        cache_version=cache_version,
        server_version=server_version,
    )


def _key() -> CacheKey:
    """Return a valid cache key, the base every case replaces one field of.

    Cases mutate this with :func:`dataclasses.replace` rather than rebuilding
    it, so each test states exactly the one field it is about and a new field
    on the key needs no change here.

    Returns
    -------
    CacheKey
        A key that is valid in every field.
    """
    return CacheKey(
        crate_name="serde",
        crate_origin="registry:serde@1.0.0",
        target="host",
        argument_digest=_VALID_DIGEST,
        toolchain_identity=_identity(),
    )


def _record(key: CacheKey) -> OutcomeRecord:
    """Return one outcome row carrying ``key``.

    Returns
    -------
    OutcomeRecord
        A row that is valid in every field but its key.
    """
    return OutcomeRecord(
        key=key,
        outcome="hit",
        origin="registry",
        runtime_seconds=0.1,
        observed_at=NOW,
        parser_class="cacheable",
        child_sighting="unavailable",
        child_samples=0,
    )


def _row_json(record: OutcomeRecord) -> bytes:
    """Return the valid encoded row for ``record`` as raw bytes.

    Returns
    -------
    bytes
        One encoded line, ready to be mutated field by field.
    """
    return encode_line(record).encode()


def test_the_valid_key_round_trips_through_the_public_path() -> None:
    """The control case proves the suite can accept a well-formed row.

    Without this, a validation change that refused every key would satisfy
    every refusal below while making the store unusable.
    """
    record = _record(_key())

    line = encode_line(record).encode()
    decoded = decode_record_line(line)

    assert not hasattr(decoded, "reason"), "a valid row is not a failure"
    assert typ.cast("OutcomeRecord", decoded).key == record.key, (
        "the key survives encode and decode unchanged"
    )


@pytest.mark.parametrize(
    "crate_name",
    ["a/b", "a\\b", "  ", ""],
    ids=["slash", "backslash", "blank", "empty"],
)
def test_a_crate_name_carrying_a_path_separator_is_refused(crate_name: str) -> None:
    """A crate name must stay a component, never a path.

    A name with a separator would name a local source location, which the store
    is built never to retain.
    """
    with pytest.raises(ValueError, match="cache key fields are invalid"):
        encode_line(_record(dc.replace(_key(), crate_name=crate_name)))


@pytest.mark.parametrize(
    "crate_origin",
    ["bogus:thing", "serde", "registry:/abs/path", "git:../relative"],
    ids=["unknown-scheme", "no-scheme", "registry-with-slash", "git-with-dotdot"],
)
def test_a_crate_origin_outside_the_closed_spellings_is_refused(
    crate_origin: str,
) -> None:
    """Only the three classified origin prefixes, without paths, are storable."""
    with pytest.raises(ValueError, match="cache key fields are invalid"):
        encode_line(_record(dc.replace(_key(), crate_origin=crate_origin)))


@pytest.mark.parametrize(
    "target",
    ["/abs/path", "../relative", "~/home", "path:notahash", "path:" + "a" * 63],
    ids=["absolute", "relative", "home", "bad-digest", "short-digest"],
)
def test_a_path_bearing_or_malformed_target_is_refused(target: str) -> None:
    """A target is a triple or a salted digest, never a raw local path."""
    with pytest.raises(ValueError, match="cache key fields are invalid"):
        encode_line(_record(dc.replace(_key(), target=target)))


@pytest.mark.parametrize(
    "digest",
    ["z" * 64, "a" * 63, "A" * 64],
    ids=["not-hex", "short", "uppercase"],
)
def test_a_malformed_argument_digest_is_refused(digest: str) -> None:
    """An argument digest is exactly 64 lowercase hexadecimal characters."""
    with pytest.raises(ValueError, match="cache key fields are invalid"):
        encode_line(_record(dc.replace(_key(), argument_digest=digest)))


@pytest.mark.parametrize(
    "identity",
    [
        _identity(compiler_digest="g" * 64),
        _identity(rustup_toolchain_digest="a" * 63),
        _identity(cache_version=""),
        _identity(server_version=""),
    ],
    ids=["compiler-not-hex", "rustup-short", "empty-cache", "empty-server"],
)
def test_a_malformed_toolchain_identity_is_refused(
    identity: ToolchainIdentity,
) -> None:
    """Every identity digest and version must be present and well formed."""
    with pytest.raises(ValueError, match="cache key fields are invalid"):
        encode_line(_record(dc.replace(_key(), toolchain_identity=identity)))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("crate_name", "a/b"),
        ("crate_origin", "bogus:thing"),
        ("target", "/abs/path"),
        ("argument_digest", "z" * 64),
    ],
    ids=["crate-name", "origin", "target", "argument-digest"],
)
def test_the_decoder_refuses_a_key_the_encoder_would_not_have_written(
    field: str,
    value: str,
) -> None:
    """A hand-edited row is corrupt even though its shape is exactly right.

    The row's field set matches the current version, so only the key checks can
    catch it. That is the path a doctored store would take.
    """
    raw = json.loads(_row_json(_record(_key())))
    raw["key"][field] = value
    doctored = json.dumps(raw, separators=(",", ":")).encode()

    decoded = decode_record_line(doctored)

    assert getattr(decoded, "reason", None) == "corrupt", (
        "a key the encoder would refuse is refused on decode too"
    )


def test_a_row_above_the_record_bound_is_refused() -> None:
    """The per-row byte bound is enforced where rows are written.

    A row is bounded so one document cannot grow without limit. The check is
    made against the encoded bytes, so a long value is refused rather than
    truncated.
    """
    oversized = _record(dc.replace(_key(), crate_name="a" * MAX_RECORD_BYTES))

    with pytest.raises(ValueError, match="record byte bound"):
        encode_line(oversized)


def test_an_invalid_row_before_the_final_row_fails_the_whole_document() -> None:
    """A bad row is refused wherever it sits, not only at the tail.

    The decoder drops only an unterminated final line, because a torn append is
    expected at a crash boundary. A malformed row that is newline-terminated is
    damage, and it must fail the document rather than be skipped.
    """
    from buildlimits.outcome_record_codec import decode_lines
    from buildlimits.outcome_record_format import RecordFailure, StoreHeader

    header = StoreHeader("fixed-salt", NOW)
    good = encode_line(_record(_key()))
    raw = json.loads(_row_json(_record(_key())))
    raw["key"]["crate_name"] = "a/b"
    bad = json.dumps(raw, separators=(",", ":"))
    document = "\n".join((encode_line(header), bad, good)) + "\n"

    decoded = decode_lines(document)

    assert isinstance(decoded, RecordFailure), "the document fails as a whole"
    assert decoded.reason == "corrupt", "a doctored middle row is corruption"


def _field_pairs() -> tuple[tuple[CacheKey, CacheKey, str], ...]:
    """Return one (broken, repaired, field) triple per refused key field.

    Each pair differs from the valid key in exactly one field, so a refusal
    cannot come from the row being broken some other way, and the repaired key
    differs from the broken one only in that same field.

    Returns
    -------
    tuple[tuple[CacheKey, CacheKey, str], ...]
        The pairs, each named by the field it exercises.
    """
    valid = _key()
    return (
        (dc.replace(valid, crate_name="a/b"), valid, "crate_name"),
        (dc.replace(valid, crate_origin="bogus:thing"), valid, "crate_origin"),
        (dc.replace(valid, target="/abs/path"), valid, "target"),
        (dc.replace(valid, argument_digest="z" * 64), valid, "argument_digest"),
    )


@pytest.mark.parametrize(
    ("broken", "repaired", "field"),
    _field_pairs(),
    ids=[field for _, _, field in _field_pairs()],
)
def test_every_refused_field_is_refused_by_itself(
    broken: CacheKey, repaired: CacheKey, field: str
) -> None:
    """Each refused field is refused by itself, and only itself.

    The broken key differs from the valid one in a single field, so the refusal
    cannot come from the row's shape. Restoring that field makes the row
    storable again, which proves the refusal tracked the field under test.
    """
    assert broken != repaired, f"the case really changes {field}"
    with pytest.raises(ValueError, match="cache key fields are invalid"):
        encode_line(_record(broken))
    assert encode_line(_record(repaired)), (
        f"restoring {field} makes the row storable again"
    )
