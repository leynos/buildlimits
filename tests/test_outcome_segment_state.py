"""Reading the sequence counter and the retention floor.

Both files are advisory. A test here pins that a damaged file is reported as
unknown rather than trusted, so gap diagnostics stay honest.
"""

from pathlib import Path

import pytest
from segment_test_helpers import store_layout, write_private

from buildlimits.outcome_segment_state import read_retention_floor, read_sequence


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


def test_retention_floor_is_read_from_state(tmp_path: Path) -> None:
    """The last successful run's floor survives a later read."""
    layout = store_layout(tmp_path)
    write_private(layout.retention_file, b'{"floor_sequence": 4}')

    floor = read_retention_floor(layout)

    assert floor.sequence == 4, "the recorded floor is reported"
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
