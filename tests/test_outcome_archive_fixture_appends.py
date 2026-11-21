"""The archive fixture waits for each appended row's own outcome.

The writer aggregates its results by operation and reason, so an append
discard no longer says which row it belongs to. A fixture that read one as a
refusal of the row it had just offered would store that row twice and lose the
discarded one. The fixture helper therefore keeps the queue from filling and
fails the test on a discard; these tests pin that decision.
"""

import errno
from pathlib import Path

import pytest
from outcome_archive_helpers import NOW, policy, records, write
from outcome_store_helpers import stored_records

from buildlimits import outcome_store_state


def test_a_discarded_row_fails_the_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker discard surfaces as a failure, never as a retry of a later row."""
    path = tmp_path / "cache-outcomes.jsonl"
    real = outcome_store_state.append_line
    refused = False

    def fail_first_append(target: Path, line: bytes) -> None:
        """Refuse the first row's write and accept every row after it."""
        nonlocal refused
        if not refused:
            refused = True
            raise OSError(errno.EIO, "injected failure")
        real(target, line)

    monkeypatch.setattr(outcome_store_state, "append_line", fail_first_append)

    with pytest.raises(pytest.fail.Exception, match="discarded 1 fixture rows"):
        write(path, records(NOW, 2), policy(1_000_000))

    stored = stored_records(path)
    assert len(set(stored)) == len(stored), "no row is stored twice"
    assert len(stored) < 2, "the refused row is not silently replaced"
