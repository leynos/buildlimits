"""Read-side contracts of the private cache-outcome store.

Reading is the side other processes depend on while a writer keeps appending:
every complete row must survive an interrupted UTF-8 tail, the file's mode must
not change, and the read must stay inside the store's byte bound even when the
writer grew the file after its size was checked.
"""

import collections.abc as cabc
import errno
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
from outcome_store_fixture_helpers import write_private_file as _write_private_file
from outcome_store_helpers import window_summary as _window_summary

from buildlimits import outcome_records, outcome_store_files, outcome_store_read
from buildlimits.outcome_store_files import open_private_regular


def test_reader_keeps_complete_rows_before_a_utf8_torn_tail(
    tmp_path: Path,
) -> None:
    """An interrupted multibyte tail cannot discard valid earlier rows."""
    path = tmp_path / "cache-outcomes.jsonl"
    record = _seed_one_record_store(path, server_version="sccache-é")
    torn_record = _window_summary(server_version="partial-é")
    _append_torn_utf8_tail(path, torn_record)

    result = outcome_store_read.read_store(path)

    assert isinstance(result, outcome_records.DecodedStore), (
        "the incomplete UTF-8 bytes are confined to the final row"
    )
    assert result.records == (record,), "the complete non-ASCII row is retained"


def test_reader_preserves_store_permissions(tmp_path: Path) -> None:
    """Read-only store inspection leaves caller-owned mode unchanged."""
    path = tmp_path / "cache-outcomes.jsonl"
    _seed_one_record_store(path)
    path.chmod(0o640)

    result = outcome_store_read.read_store(path)

    assert isinstance(result, outcome_records.DecodedStore), "the store is readable"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640, (
        "read-only inspection does not change the file mode"
    )


def test_the_private_opener_refuses_a_symlink_without_a_caller_flag(
    tmp_path: Path,
) -> None:
    """No-follow is the primitive's own rule, not each caller's duty.

    Every current caller passes ``O_NOFOLLOW`` too, but a new one could omit
    it. The opener adds the flag itself, so a substitution that reaches it is
    refused rather than followed.
    """
    target = tmp_path / "target.jsonl"
    _write_private_file(target, b"")
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)

    with pytest.raises(OSError, match="Too many") as raised:
        open_private_regular(link, os.O_RDONLY)

    assert raised.value.errno in {errno.ELOOP, errno.EMLINK}, (
        "the link is refused by the filesystem rather than followed"
    )


def test_reader_rejects_invalid_utf8_in_a_complete_line(tmp_path: Path) -> None:
    """Invalid encoding in a newline-terminated row remains store corruption."""
    path = tmp_path / "cache-outcomes.jsonl"
    _seed_one_record_store(path, server_version="sccache-é")
    original = path.read_bytes()
    malformed = original.replace("é".encode(), b"\xff\xa9", 1)
    _write_private_file(path, malformed)

    result = outcome_store_read.read_store(path)

    assert result == outcome_records.RecordFailure("corrupt"), (
        "a complete line still undergoes strict UTF-8 validation"
    )


def _fstat_reporting(reported_size: int) -> cabc.Callable[[int], os.stat_result]:
    """Build an ``fstat`` stand-in that reports a file as smaller than it is.

    The store writer appends to the active file while history readers pin it,
    so a reader can see a descriptor whose reported size predates the bytes
    now on disk. This helper models exactly that window without racing a real
    writer.

    Returns
    -------
    Callable[[int], os.stat_result]
        A replacement for ``os.fstat`` that keeps the real mode and replaces
        only the reported size.
    """
    real_fstat = os.fstat

    def stale(descriptor: int) -> os.stat_result:
        """Report the file at the size the descriptor check first saw."""
        fields = list(real_fstat(descriptor))
        fields[6] = reported_size
        return os.stat_result(fields)

    return stale


def test_reader_follows_a_store_that_grew_after_its_size_check(
    tmp_path: Path,
) -> None:
    """A row appended between the size check and the read is still returned."""
    path = tmp_path / "cache-outcomes.jsonl"
    first = _seed_one_record_store(path)
    second = _window_summary(server_version="0.15.0")
    appended = outcome_records.encode_line(second).encode("utf-8") + b"\n"
    with path.open("ab") as sink:
        sink.write(appended)

    with mock.patch.object(
        outcome_store_files.os,
        "fstat",
        _fstat_reporting(path.stat().st_size - len(appended)),
    ):
        result = outcome_store_read.read_store(path)

    assert isinstance(result, outcome_records.DecodedStore), "the grown store reads"
    assert result.records == (first, second), "the concurrently appended row is kept"


def test_reader_refuses_growth_beyond_the_store_bound(tmp_path: Path) -> None:
    """A file that grows past the bound is corrupt rather than read further."""
    path = tmp_path / "cache-outcomes.jsonl"
    _seed_one_record_store(path)
    size = path.stat().st_size
    # The descriptor reports a size under the bound while the valid store on
    # disk has since grown past it, so the refusal must come from the bound
    # rather than from a decode failure.
    with (
        mock.patch.object(outcome_records, "MAX_STORE_BYTES", size - 20),
        mock.patch.object(outcome_store_files.os, "fstat", _fstat_reporting(size - 40)),
    ):
        result = outcome_store_read.read_store(path)

    assert result == outcome_records.RecordFailure("corrupt"), (
        "growth beyond the read bound is refused even though the store is valid"
    )
