"""Repairing an interrupted final row before the store is appended to.

A writer that stopped mid-append leaves a fragment with no newline. The
decoder already skips it, but the file on disk has to match what was decoded
or the next append joins a new row to the fragment. These tests pin the
bounded truncation and the writer's use of it at startup.
"""

import datetime as dt
from pathlib import Path

from outcome_store_helpers import (
    close_writer,
    segment_writer,
    wait_for_results,
)
from outcome_test_helpers import STORE_EPOCH as NOW
from outcome_test_helpers import record_after
from segment_test_helpers import history_policy, write_private

from buildlimits.outcome_record_format import DecodedStore
from buildlimits.outcome_store_io import read_store
from buildlimits.outcome_store_tail import drop_torn_tail


def test_a_torn_tail_is_removed(tmp_path: Path) -> None:
    """The file is cut back to its last complete row."""
    path = write_private(tmp_path / "cache-outcomes.jsonl", b'{"a":1}\n{"half":')

    removed = drop_torn_tail(path)

    assert removed, "an unterminated tail is repaired"
    assert path.read_bytes() == b'{"a":1}\n', "the complete row survives"


def test_a_complete_store_is_untouched(tmp_path: Path) -> None:
    """A newline-terminated store is left exactly as it was."""
    data = b'{"a":1}\n{"b":2}\n'
    path = write_private(tmp_path / "cache-outcomes.jsonl", data)

    removed = drop_torn_tail(path)

    assert not removed, "a complete store is not a torn one"
    assert path.read_bytes() == data, "nothing is rewritten"


def test_a_store_of_only_a_fragment_is_emptied(tmp_path: Path) -> None:
    """A file with no complete row at all is truncated to nothing."""
    path = write_private(tmp_path / "cache-outcomes.jsonl", b'{"half":')

    removed = drop_torn_tail(path)

    assert removed, "the fragment is removed"
    assert path.read_bytes() == b"", "no partial row remains"


def test_an_absent_store_has_no_tail(tmp_path: Path) -> None:
    """A store that was never written is not a repair failure."""
    assert not drop_torn_tail(tmp_path / "absent.jsonl"), "absence is not damage"


def test_the_tail_is_found_beyond_one_read_chunk(tmp_path: Path) -> None:
    """The scan reads backwards in fixed chunks, so a long file still works.

    The fragment is several scan chunks long, so the last newline lies beyond
    the final chunk. A scan that looked only at that chunk, or stopped at the
    first one without a newline, would report a clean store.
    """
    rows = b'{"a":1}\n' * 20_000
    fragment = b'{"torn' * 40_000
    path = write_private(tmp_path / "cache-outcomes.jsonl", rows + fragment)

    removed = drop_torn_tail(path)

    assert removed, "the newline is found past every chunk of the fragment"
    assert path.read_bytes() == rows, "every complete row is kept"


def test_a_torn_tail_is_repaired_before_the_writer_appends(tmp_path: Path) -> None:
    """The next append cannot join its row to an interrupted fragment."""
    path = tmp_path / "cache-outcomes.jsonl"
    writer = segment_writer(path, history_policy(10**9), NOW)
    try:
        writer.append(record_after(0))
        wait_for_results(writer, operation="append", reason="none")
    finally:
        close_writer(writer)
    with path.open("ab") as sink:
        sink.write(b'{"torn":')

    writer = segment_writer(path, history_policy(10**9), NOW)
    try:
        writer.append(record_after(1))
        wait_for_results(writer, operation="append", reason="none")
    finally:
        close_writer(writer)

    decoded = read_store(path)
    assert isinstance(decoded, DecodedStore), "the repaired store decodes"
    assert [record.observed_at for record in decoded.records] == [
        NOW,
        NOW + dt.timedelta(minutes=1),
    ], "both complete rows decode"
    assert b'"torn"' not in path.read_bytes(), "the fragment never joined a row"
