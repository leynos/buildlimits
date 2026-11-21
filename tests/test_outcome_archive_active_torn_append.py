"""A torn append must not hide the committed rows before it.

The writer commits an append by its newline, so a crash can leave a fragment of
the next row instead. A scan that treats the fragment as the only uncommitted
part of the file, dropping the row before it too, reports a store smaller than
it is: the record cap passes while committed rows remain above it, and the byte
budget releases bytes those rows still occupy.
"""

import typing as typ
from pathlib import Path

from outcome_append_helpers import archived_store, run_during_paused_append
from outcome_archive_helpers import NOW, archives, document, policy, records

if typ.TYPE_CHECKING:
    import pytest

# The first bytes of an encoded row never decode on their own, so the fragment
# models a write that stopped before the writer could finish its row.
_TORN = b'{"kind": "outc'


def test_a_dropped_fragment_does_not_hide_the_row_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row before an undecodable fragment was committed and counts.

    The active file holds four committed rows and the paused append leaves only
    a fragment of a fifth. A scan that drops the last row it read along with the
    fragment reports three committed rows, so a cap of four is met by trimming
    one row from the older archive. Counting the committed row instead leaves
    two rows of excess, exactly the whole archive, which the cap evicts.
    """
    archived_store(tmp_path, retained=2, active=records(NOW, 4))

    report = run_during_paused_append(
        tmp_path,
        policy(segment_bytes=1_000_000, max_age_days=1, max_records=4),
        monkeypatch,
        in_flight=_TORN,
    )

    assert report.retained_records == 4, "the committed rows are counted"
    assert archives(tmp_path) == [], "the cap evicts the older archive"
    assert report.status == "ok", "the evicted store meets the record cap"


def test_a_dropped_fragment_does_not_release_bytes_from_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The committed rows decide the byte budget, not the dropped row.

    The budget is one byte below the committed rows beside the archive. A scan
    that dropped the last committed row would see the store within the budget
    and leave the archive unrewritten; the committed rows instead leave the
    archive one byte over, so its oldest row is trimmed in the same run.
    """
    archived_store(tmp_path, retained=2, active=records(NOW, 4))
    archived_bytes = len(document(records(NOW, 2)))
    committed_bytes = len(document(records(NOW, 4)))

    report = run_during_paused_append(
        tmp_path,
        policy(
            segment_bytes=1_000_000,
            max_age_days=1,
            max_bytes=archived_bytes + committed_bytes - 1,
        ),
        monkeypatch,
        in_flight=_TORN,
    )

    trimmed = [action for action in report.actions if action.action == "trimmed"]
    assert trimmed, "the one-byte excess trims the archive's oldest row"
    assert report.status == "ok", "the trimmed store meets the byte budget"
