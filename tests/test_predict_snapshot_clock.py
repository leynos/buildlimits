"""Clock ordering, enforced on the snapshot's write as well as its read.

The store stamps ``observed_at`` when it appends a record, so a later position
carrying an earlier stamp means the clock stepped backwards. No monotonic
sequence is stored beside the times, so the two sides of a step cannot be
separated and an admission-bearing history that crosses one is refused.
"""

import datetime as dt
from pathlib import Path

import pytest
from predict_test_helpers import hand_event, write_json

from buildlimits.predict.dataset import Snapshot, Source
from buildlimits.predict.events import EPOCH, Event
from buildlimits.predict.snapshot_codec import snapshot_to_dict
from buildlimits.predict.snapshot_store import load_snapshot, save_snapshot

NOW = dt.datetime(2026, 11, 17, 12, tzinfo=dt.UTC)


def _snapshot(*events: Event) -> Snapshot:
    """Return a snapshot holding ``events`` in append order."""
    return Snapshot(
        created_at=NOW,
        source=Source(
            salt_id="fixed-salt",
            written_at=NOW,
            records_total=len(events),
            outcome_records=len(events),
            summary_records=0,
        ),
        journal=None,
        events=events,
        gaps=(),
    )


def _backward(*, admitted: bool) -> tuple[Event, Event]:
    """Return two events whose second was observed before the first."""
    stamp = EPOCH if admitted else None
    return (
        hand_event(
            0,
            admitted_at=stamp,
            observed_at=EPOCH + dt.timedelta(minutes=10),
            label="hit",
        ),
        hand_event(
            1,
            admitted_at=stamp,
            observed_at=EPOCH + dt.timedelta(minutes=5),
            label="hit",
        ),
    )


def test_an_admission_bearing_history_with_a_backward_clock_is_refused(
    tmp_path: Path,
) -> None:
    """Across the step a label could sort before the prediction that used it."""
    document = snapshot_to_dict(_snapshot(*_backward(admitted=True)))

    with pytest.raises(ValueError, match="ran backwards"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_a_history_without_admission_times_may_report_a_backward_clock(
    tmp_path: Path,
) -> None:
    """The step only matters where admission times would order predictions."""
    document = snapshot_to_dict(_snapshot(*_backward(admitted=False)))

    loaded = load_snapshot(write_json(tmp_path / "history.jsonl", document))

    assert len(loaded.events) == 2, "the history is still readable"
    assert all(event.admitted_at is None for event in loaded.events), (
        "no admission time can be ordered by the broken clock"
    )


def test_a_backward_clock_is_refused_before_the_snapshot_is_written(
    tmp_path: Path,
) -> None:
    """A save cannot replace a good file with one its own reader refuses.

    The rule belongs to the value, not only to the reader: reporting success
    for a backward history would leave the dataset command's output unreadable
    and the earlier snapshot already gone.
    """
    path = tmp_path / "history.jsonl"

    with pytest.raises(ValueError, match="ran backwards"):
        save_snapshot(path, _snapshot(*_backward(admitted=True)))

    assert not path.exists(), "the earlier file is not replaced"
