"""A restart must never append current-format rows under an older header.

The writer emits only the current format. An active file whose loaded header is
older has to be settled before the first append, or the file becomes a document
its own older reader cannot read: the new row is reported as ``corrupt`` rather
than the store as a ``version_mismatch``.
"""

import dataclasses as dc
import datetime as dt
import json
import typing as typ
from pathlib import Path

from outcome_sealing_helpers import active_of, sealed_names
from outcome_store_clock import FIXED_NOW
from outcome_store_helpers import (
    append_and_wait,
    salt_path,
    segment_writer,
    start_writer,
    wait_for_results,
    wait_for_salt,
    worker_exited,
)
from outcome_test_helpers import OBSERVED_AT, attributed_record, record_after
from outcome_test_helpers import STORE_EPOCH as NOW
from segment_test_helpers import (
    history_policy,
    version_one_document,
    version_two_document,
    write_private,
)

from buildlimits import outcome_record_codec, outcome_record_format
from buildlimits.outcome_key import Salt
from buildlimits.outcome_record_encode import encode_line
from buildlimits.outcome_record_format import (
    FORMAT_VERSION,
    DecodedStore,
    RecordFailure,
    StoreHeader,
)
from buildlimits.outcome_store_salt import file_salt

if typ.TYPE_CHECKING:
    import pytest

_ROW_MINUTE = 5


def _seed_version_one_store(path: Path, rows: int) -> Salt:
    """Write one version 1 store under the salt the writer will activate.

    Returns
    -------
    Salt
        The salt domain the seeded rows were keyed under.
    """
    salt = file_salt(salt_path(path))
    assert isinstance(salt, Salt), "the fixture installs its own salt"
    records = tuple(record_after(minute) for minute in range(rows))
    write_private(path, version_one_document(records, salt.salt_id))
    return salt


def _upgraded_document(store: Path, *, segmented: bool) -> str:
    """Restart a version 1 store, append one row and return the file text.

    Returns
    -------
    str
        The store's document after the upgrade and the append.
    """
    record = record_after(_ROW_MINUTE)
    if segmented:
        writer = segment_writer(store, history_policy(1 << 20), FIXED_NOW)
        try:
            append_and_wait(writer, record)
        finally:
            writer.close(1.0)
    else:
        writer = start_writer(store)
        try:
            wait_for_salt(writer)
            wait_for_results(writer, operation="compact")
            append_and_wait(writer, record)
        finally:
            writer.close(1.0)
    assert worker_exited(writer, 2.0), "the writer stops within its bound"
    return store.read_text(encoding="utf-8")


def test_a_segmented_restart_seals_a_version_one_active_file(
    tmp_path: Path,
) -> None:
    """A small, unexpired, same-salt version 1 file is closed, not appended to.

    The file is below the segment bound and inside the age bound, so nothing
    else in the settle pass would touch it. Without the upgrade rule the first
    current-format row would be written beneath the version 1 header.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    salt = _seed_version_one_store(store, 1)

    _upgraded_document(store, segmented=True)

    assert sealed_names(store) == ["segment-000000000001.jsonl"], (
        "the old document is closed as one segment rather than rewritten away"
    )
    active = active_of(store)
    assert active.header.version == FORMAT_VERSION, "the active header is current"
    assert active.header.salt_id == salt.salt_id, "the same salt domain continues"
    assert len(active.records) == 1, "the new row is the only row in the file"
    assert active.records[0].observed_at == record_after(_ROW_MINUTE).observed_at, (
        "the appended row is the one the test supplied"
    )


def test_a_current_reader_decodes_the_upgraded_file(tmp_path: Path) -> None:
    """No version 1 header remains above a current-format row."""
    store = tmp_path / "cache-outcomes.jsonl"
    _seed_version_one_store(store, 1)

    document = _upgraded_document(store, segmented=True)

    versions = {
        json.loads(line)["version"]
        for line in document.splitlines()
        if "version" in json.loads(line)
    }
    assert versions == {FORMAT_VERSION}, "every header in the file is current"
    decoded = outcome_record_codec.decode_lines(document)
    assert isinstance(decoded, DecodedStore), decoded
    assert len(decoded.records) == 1, "the appended row decodes"


def test_an_older_reader_names_the_version_not_a_corrupt_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A downgraded reader refuses the format before it reads any row.

    The supported-version set is the one a version 1 release shipped, so this
    reproduces the rule it applies to the header of the upgraded file. The
    distinction matters: a version mismatch moves the store aside for an
    operator, while a corrupt row looks like damage to history the store
    actually holds.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    _seed_version_one_store(store, 1)

    document = _upgraded_document(store, segmented=True)
    header_line = document.splitlines()[0]
    assert json.loads(header_line)["version"] > 1, "the file was upgraded"

    monkeypatch.setattr(
        outcome_record_format, "SUPPORTED_FORMAT_VERSIONS", frozenset({1})
    )
    refused = outcome_record_codec.decode_header_line(header_line.encode())

    assert isinstance(refused, RecordFailure), refused
    assert refused.reason == "version_mismatch", (
        "an older reader names the version, never a corrupt row"
    )


def test_the_legacy_path_rewrites_an_older_header_before_appending(
    tmp_path: Path,
) -> None:
    """A compacting store installs a current header rather than appending above one."""
    store = tmp_path / "cache-outcomes.jsonl"
    _seed_version_one_store(store, 1)

    document = _upgraded_document(store, segmented=False)

    header = json.loads(document.splitlines()[0])
    assert header["version"] == FORMAT_VERSION, (
        "the compacted document's header is current"
    )
    decoded = outcome_record_codec.decode_lines(document)
    assert isinstance(decoded, DecodedStore), decoded
    assert len(decoded.records) == 2, "the seeded row is kept beside the new one"


def test_a_header_round_trips_the_version_it_names() -> None:
    """Encoding preserves the header's own version rather than imposing one."""
    written = StoreHeader("salt-1", NOW, version=1)

    decoded = outcome_record_codec.decode_header_line(encode_line(written).encode())

    assert isinstance(decoded, StoreHeader), decoded
    assert decoded.version == 1, "a decoded header keeps the file's own version"
    assert decoded == written, "every header field survives the round trip"
    current = dc.replace(written, version=FORMAT_VERSION)
    assert json.loads(encode_line(current))["version"] == FORMAT_VERSION, (
        "the current format is written when the header names it"
    )


_ADMITTED_AT = OBSERVED_AT - dt.timedelta(seconds=2)


def _evidenced_row() -> outcome_record_format.OutcomeRecord:
    """Return one row carrying every field version 2 introduced.

    Returns
    -------
    outcome_record_format.OutcomeRecord
        A row with an admission time, an outcome reason and an attribution.
    """
    return dc.replace(
        attributed_record(outcome="miss"),
        outcome_reason="mixed_window",
        outcome_attribution="none",
        admitted_at=_ADMITTED_AT,
    )


def test_a_version_two_fixture_keeps_the_evidence_version_two_added() -> None:
    """The version 2 fixture omits only version 3's provenance marker.

    A fixture that also stripped the admission time, reason and attribution
    would reproduce a version 1 row shape under a version 2 header, so no test
    built on it could detect that evidence going missing.
    """
    row = _evidenced_row()

    raw = json.loads(version_two_document((row,)).splitlines()[1])
    decoded = outcome_record_codec.decode_lines(version_two_document((row,)).decode())

    assert isinstance(decoded, DecodedStore), decoded
    assert decoded.header.version == 2, "the fixture writes the version it names"
    (stored,) = decoded.records
    assert isinstance(stored, outcome_record_format.OutcomeRecord), stored
    assert stored.admitted_at == _ADMITTED_AT, "the admission time is kept"
    assert stored.outcome_reason == "mixed_window", "the reason is kept"
    assert stored.outcome_attribution == "none", "the attribution is kept"
    assert "admitted_at" in raw and raw["admitted_at"] is not None, (
        "the evidence is in the bytes, not merely decoded by the current reader"
    )
    assert "label_time_provenance" not in raw, "version 2 cannot state the marker"


def test_a_version_three_document_carries_the_label_time_marker() -> None:
    """A current document states the provenance a version 2 row cannot."""
    row = dc.replace(_evidenced_row(), label_time_provenance="verified")

    encoded = "\n".join((encode_line(StoreHeader("salt-1", NOW)), encode_line(row)))
    raw = json.loads(encoded.splitlines()[1])
    decoded = outcome_record_codec.decode_lines(encoded + "\n")

    assert isinstance(decoded, DecodedStore), decoded
    assert decoded.header.version == FORMAT_VERSION, "the header is current"
    (stored,) = decoded.records
    assert isinstance(stored, outcome_record_format.OutcomeRecord), stored
    assert stored.label_time_provenance == "verified", (
        "the row states the provenance version 3 added"
    )
    assert raw["label_time_provenance"] == "verified", (
        "the marker is in the bytes the fixture writes"
    )
