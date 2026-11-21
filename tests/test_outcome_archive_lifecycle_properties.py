"""Generated lifecycle transitions, replayed and gap-checked after each.

A closed segment's life is a small sequence of transitions: the writer seals
it, the archiver compresses and publishes it, and a later firing may evict it.
The deterministic tests pin each transition's decision; these properties drive
the real archiver over generated histories and assert what an operator reads
afterwards: every surviving row replays in order, a sequence that went missing
is reported exactly once as a gap, and a second firing changes neither. One
generated history withholds its last sealed source before the run, so the gap
branch is exercised rather than assumed.
"""

import datetime as dt
import os
import typing as typ
from pathlib import Path

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from outcome_test_helpers import records_at
from segment_test_helpers import document, write_private

from buildlimits.outcome_history import read_history
from buildlimits.outcome_segment_inventory import read_inventory
from buildlimits.outcome_segment_state import read_retention_floor
from buildlimits.outcome_segments import SegmentLayout, archive_name

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import StoreRecord

from outcome_archive_helpers import NOW, policy, run

# A gap is only reportable strictly between two surviving sequences. A fresh
# store derives its floor from the lowest sequence it still holds, so a
# withheld first sequence reads as a late start rather than a loss; a withheld
# last sequence sits above every survivor, which is indistinguishable from a
# store that simply ended there. The generated history therefore has at least
# three segments and withholds a middle one, and a shorter one withholds
# nothing.
_GAP_CHOICE = st.one_of(st.none(), st.integers(min_value=0, max_value=2))


def _withheld_sequence(counts: list[int], choice: int | None) -> int | None:
    """Return a middle sequence to withhold, or None to withhold nothing.

    Returns
    -------
    int | None
        A sequence with a surviving entry both before and after it, or None.
    """
    if choice is None or len(counts) < 3:
        return None
    return 2 + choice % (len(counts) - 2)


def _rows_for(sequence: int, count: int) -> tuple[StoreRecord, ...]:
    """Return one segment's rows, distinct from every other segment's.

    Returns
    -------
    tuple[StoreRecord, ...]
        Rows observed ten minutes apart, offset by the sequence.
    """
    return records_at(NOW + dt.timedelta(minutes=10 * sequence), count)


def _build_store(
    root: Path, counts: list[int], withheld: int | None
) -> tuple[SegmentLayout, dict[int, tuple[StoreRecord, ...]]]:
    """Write one sealed source per generated count and withhold one of them.

    Returns
    -------
    tuple[SegmentLayout, dict[int, tuple[StoreRecord, ...]]]
        The store's layout and the rows each surviving sequence holds.
    """
    store_layout = SegmentLayout.beside(root / "cache-outcomes.jsonl")
    surviving: dict[int, tuple[StoreRecord, ...]] = {}
    for sequence, count in enumerate(counts, start=1):
        rows = _rows_for(sequence, count)
        if sequence == withheld:
            continue
        path = store_layout.sealed_dir / archive_name(sequence).removesuffix(".xz")
        write_private(path, document(rows))
        surviving[sequence] = rows
    return store_layout, surviving


def _assert_replayed(
    store_layout: SegmentLayout, surviving: dict[int, tuple[StoreRecord, ...]]
) -> None:
    """Assert retained history is exactly the surviving rows, in order."""
    expected = tuple(row for rows in surviving.values() for row in rows)
    assert read_history(store_layout).records == expected, (
        "every surviving row replays once, in sequence order"
    )


def _assert_gaps(store_layout: SegmentLayout, withheld: int | None) -> None:
    """Assert the inventory reports exactly the withheld sequence."""
    floor = read_retention_floor(store_layout).sequence
    inventory = read_inventory(store_layout, retention_floor=floor)
    expected = () if withheld is None else (withheld,)
    assert inventory.gaps == expected, "a missing sequence is reported once"
    assert inventory.gaps_truncated is False, "a generated history fits the list"


@given(
    counts=st.lists(st.integers(min_value=0, max_value=4), min_size=1, max_size=3),
    choice=_GAP_CHOICE,
)
@example(counts=[2, 0], choice=None)
@example(counts=[0, 0, 0], choice=0)
@example(counts=[1, 3, 0], choice=1)
@settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_generated_lifecycles_replay_and_report_gaps(
    tmp_path: Path, counts: list[int], choice: int | None
) -> None:
    """Archiving preserves replay and reports a withheld sequence as a gap."""
    withheld = _withheld_sequence(counts, choice)
    root = tmp_path / os.urandom(4).hex()
    store_layout, surviving = _build_store(root, counts, withheld)

    report = run(root, policy(max_age_days=0))

    assert report.status == "ok", "a generated history archives successfully"
    assert not read_inventory(store_layout).sealed, "no source is left behind"
    _assert_replayed(store_layout, surviving)
    _assert_gaps(store_layout, withheld)

    again = run(root, policy(max_age_days=0))

    assert again.status == "ok", "a repeat firing is a no-op success"
    _assert_replayed(store_layout, surviving)
    _assert_gaps(store_layout, withheld)
