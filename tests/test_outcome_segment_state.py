"""Reading the sequence counter and the retention floor.

Both files are advisory. A test here pins that a damaged file is reported as
unknown rather than trusted, so gap diagnostics stay honest.
"""

from pathlib import Path

import pytest
from segment_test_helpers import store_layout, write_private

from buildlimits import outcome_segment_state
from buildlimits.outcome_segment_state import (
    RetentionFloor,
    read_retention_floor,
    read_sequence,
)
from buildlimits.outcome_segments import MAX_SEQUENCE


def test_sequence_counter_accepts_surrounding_whitespace(tmp_path: Path) -> None:
    """A counter written with a trailing newline still reads as one integer."""
    path = write_private(tmp_path / "cache-outcomes.seq", b" 12 \n")

    assert read_sequence(path) == 12, "whitespace is not part of the value"


def test_a_missing_counter_is_not_a_fault(tmp_path: Path) -> None:
    """An unwritten counter means the store has never sealed."""
    assert read_sequence(tmp_path / "absent.seq") is None, "absence is not an error"


@pytest.mark.parametrize(
    "data",
    [b"not-a-number", b"12x", b"", b"1" * 40, b"\xff\xfe"],
    ids=["letters", "trailing-garbage", "empty", "oversized", "non-ascii"],
)
def test_a_counter_that_is_not_one_decimal_integer_is_a_fault(
    tmp_path: Path, data: bytes
) -> None:
    """Anything but a bounded decimal integer is rejected, never guessed."""
    path = write_private(tmp_path / "cache-outcomes.seq", data)

    with pytest.raises(ValueError, match="sequence counter"):
        read_sequence(path)


def test_a_counter_beyond_the_name_width_is_a_fault(tmp_path: Path) -> None:
    """An out-of-range counter would name a segment the inventory cannot see.

    Accepting it would make every later seal publish a source the twelve-digit
    scan ignores, so the counter is reported as damaged instead. The writer's
    exhausted marker sits one past the largest name and is readable state, so
    the damaged value is the one beyond it.
    """
    path = write_private(
        tmp_path / "cache-outcomes.seq", f"{MAX_SEQUENCE + 2}\n".encode()
    )

    with pytest.raises(ValueError, match="out of range"):
        read_sequence(path)


def test_the_exhausted_marker_is_readable_state(tmp_path: Path) -> None:
    """The writer's exhausted counter is not damage.

    One past the largest name is what the writer records after sealing it. A
    reader that rejected the value would fall back to directory names, which
    retention can empty, and a later seal could reuse a sequence.
    """
    path = write_private(
        tmp_path / "cache-outcomes.seq", f"{MAX_SEQUENCE + 1}\n".encode()
    )

    assert read_sequence(path) == MAX_SEQUENCE + 1, "the marker is reported as read"


def test_the_largest_nameable_sequence_is_accepted(tmp_path: Path) -> None:
    """The bound itself is still a sequence the store can name."""
    path = write_private(tmp_path / "cache-outcomes.seq", f"{MAX_SEQUENCE}\n".encode())

    assert read_sequence(path) == MAX_SEQUENCE, "the last nameable counter reads"


def test_a_directory_is_not_a_sequence_counter(tmp_path: Path) -> None:
    """A non-regular file cannot describe the next sequence."""
    (tmp_path / "cache-outcomes.seq").mkdir()

    with pytest.raises(ValueError, match="not a regular file"):
        read_sequence(tmp_path / "cache-outcomes.seq")


def test_a_symlinked_counter_is_a_fault(tmp_path: Path) -> None:
    """A link out of the store is never followed to read the counter."""
    target = write_private(tmp_path / "elsewhere.seq", b"12\n")
    path = tmp_path / "cache-outcomes.seq"
    path.symlink_to(target)

    with pytest.raises(ValueError, match="sequence counter"):
        read_sequence(path)


@pytest.mark.parametrize(
    ("primary", "pending", "expected"),
    [
        (b'{"floor_sequence": 4}', None, 4),
        (b'{"floor_sequence": 2}', b'{"floor_sequence": 5}', 5),
        (None, b'{"floor_sequence": 4}', 4),
        (b'{"floor_sequence": 4}', b"not json", 4),
    ],
    ids=["primary-only", "higher-wins", "pending-only", "damaged-pending"],
)
def test_the_retention_floor_is_the_higher_valid_record(
    tmp_path: Path,
    primary: bytes | None,
    pending: bytes | None,
    expected: int,
) -> None:
    """The last successful run's floor survives a later read.

    A run that cannot replace ``retention.json`` leaves the pending record
    instead, so both are read and the higher valid floor wins. A damaged
    record never discards the floor the other one still holds.
    """
    layout = store_layout(tmp_path)
    for path, data in (
        (layout.retention_file, primary),
        (layout.pending_retention_file, pending),
    ):
        if data is not None:
            write_private(path, data)

    floor = read_retention_floor(layout)

    assert floor.sequence == expected, "the recorded floor is reported"
    assert floor.state == "recorded", "a floor this archiver wrote is trusted"


def test_an_absent_retention_record_is_missing_not_damaged(tmp_path: Path) -> None:
    """A store that never recorded a floor is not a store with damaged state."""
    floor = read_retention_floor(store_layout(tmp_path))

    assert floor.sequence is None, "nothing is recorded"
    assert floor.state == "missing", "absence is distinguished from damage"


def test_an_unreadable_retention_record_is_named_unreadable(tmp_path: Path) -> None:
    """A record that cannot be opened is reported as an I/O fault, not ignored."""
    layout = store_layout(tmp_path)
    layout.retention_file.parent.mkdir(parents=True)
    layout.retention_file.mkdir()

    floor = read_retention_floor(layout)

    assert floor.sequence is None, "no floor is trusted from an unread file"
    assert floor.state == "unreadable", "the read fault keeps its own name"


@pytest.mark.parametrize(
    "document",
    [
        b'{"floor_sequence": -1}',
        b'{"floor_sequence": true}',
        b'{"floor_sequence": "4"}',
        b'{"other": 4}',
        b"not json",
        b"[]",
    ],
    ids=["negative", "boolean", "text", "missing", "malformed", "sequence"],
)
def test_unusable_retention_state_is_invalid(tmp_path: Path, document: bytes) -> None:
    """A damaged floor never suppresses real gap diagnostics."""
    layout = store_layout(tmp_path)
    write_private(layout.retention_file, document)

    floor = read_retention_floor(layout)

    assert floor.sequence is None, "no floor is trusted from damage"
    assert floor.state == "invalid", "damaged content is named as invalid"


def test_an_oversized_retention_record_is_not_read(tmp_path: Path) -> None:
    """A huge file in the state directory cannot be pulled into memory.

    The document is valid and only padded past the bound, so an unbounded read
    would accept it and report its floor rather than fail to parse it.
    """
    layout = store_layout(tmp_path)
    document = b'{"floor_sequence": 4}'
    write_private(layout.retention_file, document + b" " * (4096 - len(document) + 1))

    floor = read_retention_floor(layout)

    assert floor.sequence is None, "the record is refused unread"
    assert floor.state == "invalid", "an oversized record is not this code's state"


def test_the_fallback_record_is_read_before_the_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent install cannot make the floor read lower than it is.

    A run installs ``retention.json`` before it removes
    ``retention-pending.json``. Reading the fallback first means a read that
    sees no fallback also sees the installed primary; the reverse order can
    take an old primary and then miss the removed fallback.
    """
    layout = store_layout(tmp_path)
    calls: list[str] = []

    def fake(path: Path) -> RetentionFloor:
        """Name the order and return the higher floor from the fallback."""
        calls.append(path.name)
        sequence = 5 if path.name == layout.pending_retention_file.name else 2
        return RetentionFloor(sequence=sequence, state="recorded")

    monkeypatch.setattr(outcome_segment_state, "_floor_in", fake)

    floor = outcome_segment_state.read_retention_floor(layout)

    assert calls == [
        layout.pending_retention_file.name,
        layout.retention_file.name,
    ], "the fallback is read before the primary"
    assert floor.sequence == 5, "the higher recorded floor wins"
