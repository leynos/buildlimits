"""Asynchronous outcome-store ownership, bounds and privacy contracts."""

import os
import stat
from pathlib import Path
from unittest import mock

import pytest
from outcome_store_fixture_helpers import (
    append_torn_utf8_tail as _append_torn_utf8_tail,
)
from outcome_store_fixture_helpers import (
    seed_one_record_store as _seed_one_record_store,
)
from outcome_store_helpers import append_and_wait as _append_and_wait
from outcome_store_helpers import assert_failed_append as _assert_failed_append
from outcome_store_helpers import assert_load_result as _assert_load_result
from outcome_store_helpers import decoded_store as _decoded_store
from outcome_store_helpers import load_and_close as _load_and_close
from outcome_store_helpers import running_writer as _running_writer
from outcome_store_helpers import salt_path as _salt_path
from outcome_store_helpers import seed_store as _seed_store
from outcome_store_helpers import start_writer as _start_writer
from outcome_store_helpers import stored_records as _stored_records
from outcome_store_helpers import wait_for_results as _wait_for_results
from outcome_store_helpers import wait_for_salt as _wait_for_salt
from outcome_store_helpers import window_summary as _window_summary
from outcome_store_helpers import write_private_file as _write_private_file

from buildlimits import outcome_records, outcome_store, outcome_store_io
from buildlimits.outcome_key import Salt
from buildlimits.outcome_ports import StoreResult
from buildlimits.outcome_record_format import StoreHeader, StoreRecord, WindowSummary
from buildlimits.outcome_store import file_salt

_BAD_SUFFIX = ".bad"


def test_writer_creates_private_header_and_publishes_salt(tmp_path: Path) -> None:
    """The writer creates a private store and publishes its salt asynchronously."""
    path = tmp_path / "state" / "buildlimits" / "cache-outcomes.jsonl"
    writer = _start_writer(path)
    try:
        salt = _wait_for_salt(writer)
        _wait_for_results(writer, operation="compact", reason="none")
    finally:
        writer.close(1.0)

    assert isinstance(salt, Salt), "the writer sends key material after initialization"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700, (
        "the store directory is private"
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600, "the JSON Lines file is private"
    assert stat.S_IMODE(_salt_path(path).stat().st_mode) == 0o600, (
        "the salt file is private"
    )
    decoded = _decoded_store(path)
    assert decoded.header.salt_id == salt.salt_id, "the header names the active salt"
    assert decoded.records == (), "a new store starts without records"


def test_writer_appends_a_record_and_compacts_on_close(tmp_path: Path) -> None:
    """Queued records reach the file before the bounded close completes."""
    path = tmp_path / "cache-outcomes.jsonl"
    with _running_writer(path) as writer:
        _append_and_wait(writer, _window_summary())

    decoded = _decoded_store(path)
    assert len(decoded.records) == 1, "the accepted record survives close compaction"
    assert isinstance(decoded.records[0], WindowSummary), "the typed row is a summary"
    assert decoded.records[0].server_version == "0.14.0", "the typed row round-trips"


def test_store_reader_rejects_a_fifo_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a FIFO before its open can wait for a peer process."""
    path = tmp_path / "cache-outcomes.jsonl"
    os.mkfifo(path)
    open_file = os.open

    def open_nonblocking(candidate: Path, flags: int, mode: int = 0o777) -> int:
        """Fail safely if the store reader omits the nonblocking flag."""
        if candidate == path and not flags & os.O_NONBLOCK:
            pytest.fail("store paths must be opened with O_NONBLOCK")
        return open_file(candidate, flags, mode)

    monkeypatch.setattr(outcome_store_io.os, "open", open_nonblocking)
    with pytest.raises(OSError, match="not a regular file"):
        outcome_store_io.read_store(path)


def test_store_temp_close_failure_does_not_close_the_descriptor_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed close must not retry a descriptor that may already be reused."""
    temporary = tmp_path / ".cache-outcomes.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    close_file = os.close
    close_calls = 0

    def close_then_raise(candidate: int) -> None:
        """Model close reporting an error after releasing its descriptor."""
        nonlocal close_calls
        close_calls += 1
        close_file(candidate)
        raise OSError("close reported an I/O error")

    monkeypatch.setattr(outcome_store_io.os, "close", close_then_raise)
    with pytest.raises(OSError, match="close reported an I/O error"):
        outcome_store_io.write_private_temp(temporary, descriptor, b"record\n")

    assert close_calls == 1, "the possibly reused descriptor is never closed again"
    assert not temporary.exists(), "a failed write removes its temporary file"


def test_append_compacts_at_the_high_water_mark(tmp_path: Path) -> None:
    """High-water compaction evicts the oldest row before the next append."""
    path = tmp_path / "cache-outcomes.jsonl"
    older = _window_summary(server_version="v" * 6_000)
    newer = _window_summary()
    with _running_writer(path) as writer:
        _append_and_wait(writer, older)
        header = _decoded_store(path).header
        compact_limit = sum(
            len(outcome_records.encode_line(value).encode("utf-8")) + 1
            for value in (header, newer)
        )

        with mock.patch.object(outcome_records, "MAX_COMPACT_BYTES", compact_limit):
            writer.append(newer)
            compact_results = _wait_for_results(
                writer, operation="compact", reason="none"
            )
            if not any(result.operation == "append" for result in compact_results):
                compact_results += _wait_for_results(
                    writer, operation="append", reason="none"
                )

    assert StoreResult("compact", "none", 0, 1) in compact_results, (
        "the old row is counted as byte-budget eviction"
    )
    decoded = _decoded_store(path)
    assert decoded.records == (newer,), "the new row fits the compacted byte budget"


def test_oversized_record_is_dropped_without_touching_the_store(
    tmp_path: Path,
) -> None:
    """A record over 16 KiB is rejected before it reaches the writer queue."""
    path = tmp_path / "cache-outcomes.jsonl"
    with _running_writer(path) as writer:
        writer.append(_window_summary(server_version="v" * 16_300))
        results = _wait_for_results(writer, operation="append", reason="save_failed")

    _assert_failed_append(path, results, ())


@pytest.mark.parametrize(
    "allow_append", [False, True], ids=("hard-limit", "within-limit")
)
def test_compaction_failure_obeys_the_16_mib_hard_limit(
    tmp_path: Path, allow_append: bool
) -> None:
    """A failed compaction permits only a write within the configured hard bound."""
    path = tmp_path / "cache-outcomes.jsonl"
    with _running_writer(path) as writer:
        _append_and_wait(writer, _window_summary())
        prior_size = path.stat().st_size
        new_record = _window_summary(server_version="v" * 200)
        new_line_size = len(outcome_records.encode_line(new_record).encode("utf-8")) + 1
        resulting_size = prior_size + new_line_size
        hard_limit = resulting_size if allow_append else resulting_size - 1

        with (
            mock.patch.object(outcome_records, "MAX_COMPACT_BYTES", prior_size - 1),
            mock.patch.object(outcome_records, "MAX_STORE_BYTES", hard_limit),
            mock.patch.object(
                outcome_store, "_replace_store", side_effect=OSError("disk full")
            ),
        ):
            writer.append(new_record)
            reason = "none" if allow_append else "save_failed"
            _wait_for_results(writer, operation="append", reason=reason)

    expected_size = resulting_size if allow_append else prior_size
    assert path.stat().st_size == expected_size, (
        "the failed compaction respects the hard cap"
    )


def test_writer_drops_only_a_torn_final_line_on_load(tmp_path: Path) -> None:
    """Startup compaction preserves complete rows and removes a torn tail."""
    path = tmp_path / "cache-outcomes.jsonl"
    record = _seed_one_record_store(path, torn_tail='{"kind":"window_summary"')

    writer = _start_writer(path)
    _wait_for_salt(writer)
    _wait_for_results(writer, operation="compact", reason="none")
    writer.close(1.0)

    assert _stored_records(path) == (record,), "the partial append is dropped"
    assert path.read_bytes().endswith(b"\n"), "compaction leaves a complete final line"


def test_reader_keeps_complete_rows_before_a_utf8_torn_tail(
    tmp_path: Path,
) -> None:
    """An interrupted multibyte tail cannot discard valid earlier rows."""
    path = tmp_path / "cache-outcomes.jsonl"
    record = _seed_one_record_store(path, server_version="sccache-é")
    torn_record = _window_summary(server_version="partial-é")
    _append_torn_utf8_tail(path, torn_record)

    result = outcome_store_io.read_store(path)

    assert isinstance(result, outcome_records.DecodedStore), (
        "the incomplete UTF-8 bytes are confined to the final row"
    )
    assert result.records == (record,), "the complete non-ASCII row is retained"


def test_reader_preserves_store_permissions(tmp_path: Path) -> None:
    """Read-only store inspection leaves caller-owned mode unchanged."""
    path = tmp_path / "cache-outcomes.jsonl"
    _seed_one_record_store(path)
    path.chmod(0o640)

    result = outcome_store_io.read_store(path)

    assert isinstance(result, outcome_records.DecodedStore), "the store is readable"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640, (
        "read-only inspection does not change the file mode"
    )


def test_reader_rejects_invalid_utf8_in_a_complete_line(tmp_path: Path) -> None:
    """Invalid encoding in a newline-terminated row remains store corruption."""
    path = tmp_path / "cache-outcomes.jsonl"
    _seed_one_record_store(path, server_version="sccache-é")
    original = path.read_bytes()
    malformed = original.replace("é".encode(), b"\xff\xa9", 1)
    _write_private_file(path, malformed)

    result = outcome_store_io.read_store(path)

    assert result == outcome_records.RecordFailure("corrupt"), (
        "a complete line still undergoes strict UTF-8 validation"
    )


def test_startup_compaction_is_retried_before_appending_after_failure(
    tmp_path: Path,
) -> None:
    """A transient compaction fault cannot join a record to a torn tail."""
    path = tmp_path / "cache-outcomes.jsonl"
    previous = _seed_one_record_store(
        path,
        server_version="0.13.0",
        torn_tail='{"kind":"window_summary"',
    )
    appended = _window_summary(server_version="0.14.0")

    replace_store = outcome_store._replace_store
    compaction_attempts = 0

    def fail_first_compaction(
        store_path: Path,
        header: StoreHeader,
        records: tuple[StoreRecord, ...],
    ) -> None:
        """Fail startup once, then allow the retry to repair the tail."""
        nonlocal compaction_attempts
        compaction_attempts += 1
        if compaction_attempts == 1:
            raise OSError("temporary compaction failure")
        replace_store(store_path, header, records)

    with mock.patch.object(
        outcome_store, "_replace_store", side_effect=fail_first_compaction
    ):
        writer = _start_writer(path)
        try:
            _wait_for_salt(writer)
            startup = _wait_for_results(
                writer, operation="compact", reason="save_failed"
            )
            assert StoreResult("compact", "save_failed", 0, 0) in startup, (
                "startup reports the transient compaction fault"
            )
            writer.append(appended)
            results = _wait_for_results(writer, operation="append", reason="none")
            assert StoreResult("append", "none", 1, 0) in results, (
                "the append follows a successful compaction retry"
            )
            assert _stored_records(path) == (previous, appended), (
                "the append does not corrupt the store before shutdown"
            )
        finally:
            writer.close(1.0)


def test_writer_moves_a_store_over_16_mib_as_corrupt(tmp_path: Path) -> None:
    """Reject an oversized file without reading or appending to its contents."""
    path = tmp_path / "cache-outcomes.jsonl"
    file_salt(_salt_path(path))
    oversized = b"x" * (outcome_records.MAX_STORE_BYTES + 1)
    _write_private_file(path, oversized)

    _, results = _load_and_close(path, reason="corrupt", compact=True)

    _assert_load_result(results, "corrupt")
    assert path.with_name(path.name + _BAD_SUFFIX).read_bytes() == oversized, (
        "the original file is preserved aside"
    )
    assert _stored_records(path) == (), "the replacement store is empty"


def test_writer_moves_a_version_mismatch_aside_once(tmp_path: Path) -> None:
    """Preserve a newer format while creating the current header separately."""
    path = tmp_path / "cache-outcomes.jsonl"
    salt = file_salt(_salt_path(path))
    assert isinstance(salt, Salt), "the fixture uses a readable salt"
    future = (
        '{"format":"buildlimits-cache-outcomes","version":2,'
        f'"salt_id":"{salt.salt_id}","written_at":"2026-10-25T00:00:00Z"}}\n'
    ).encode()
    _write_private_file(path, future)

    active_salt, results = _load_and_close(
        path, reason="version_mismatch", compact=True
    )

    assert isinstance(active_salt, Salt), "the replacement salt remains readable"
    _assert_load_result(results, "version_mismatch")
    assert path.with_name(path.name + _BAD_SUFFIX).read_bytes() == future, (
        "the newer format is preserved exactly"
    )
    current = _decoded_store(path)
    assert current.header.salt_id == salt.salt_id == active_salt.salt_id, (
        "the replacement uses the active salt"
    )


def test_writer_preserves_both_files_when_bad_backup_already_exists(
    tmp_path: Path,
) -> None:
    """A second invalid store cannot overwrite the one permitted backup."""
    path = tmp_path / "cache-outcomes.jsonl"
    file_salt(_salt_path(path))
    future = b'{"format":"buildlimits-cache-outcomes","version":2}\n'
    backup = path.with_name(path.name + _BAD_SUFFIX)
    _write_private_file(backup, b"previous backup")
    _write_private_file(path, future)

    _, results = _load_and_close(path, reason="version_mismatch")

    _assert_load_result(results, "version_mismatch")
    assert path.read_bytes() == future, "the second unsupported file is not overwritten"
    assert backup.read_bytes() == b"previous backup", "the first backup is preserved"


def test_writer_leaves_an_unreadable_store_object_untouched(tmp_path: Path) -> None:
    """A non-file store path is reported and never replaced."""
    path = tmp_path / "cache-outcomes.jsonl"
    path.mkdir()
    _, results = _load_and_close(path, reason="unreadable")

    _assert_load_result(results, "unreadable")
    assert path.is_dir(), "the pre-existing store object is preserved"


def test_changed_salt_discards_old_keyed_records(tmp_path: Path) -> None:
    """A changed salt starts a fresh history rather than comparing old keys."""
    path = tmp_path / "cache-outcomes.jsonl"
    salt_path = _salt_path(path)
    old_salt = file_salt(salt_path)
    assert isinstance(old_salt, Salt), "the fixture gets its first salt"
    record = _window_summary()
    _seed_store(path, old_salt, (record,))
    _write_private_file(salt_path, b"n" * 32)

    new_salt, results = _load_and_close(path, reason="salt_changed", compact=True)

    assert isinstance(new_salt, Salt), "the replacement salt is readable"
    _assert_load_result(results, "salt_changed", discarded=1)
    decoded = _decoded_store(path)
    assert decoded.header.salt_id == new_salt.salt_id, "the header uses the new salt"
    assert decoded.records == (), "old keyed rows are excluded from the new history"
