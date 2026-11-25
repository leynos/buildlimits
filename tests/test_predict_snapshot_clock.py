"""Clock ordering, enforced on the snapshot's write as well as its read.

The recorder stamps every row of one statistics reading with that reading's
completion moment, so rows a store appended together share a time. A later
position carrying an earlier stamp therefore means the wall clock stepped
backwards. No monotonic sequence is stored beside the times, so the two sides
of a step cannot be separated and an admission-bearing history that crosses one
is refused. Two launches finishing out of order no longer create such a step:
they share the reading's moment, which the last test below pins.
"""

import dataclasses as dc
import datetime as dt
from pathlib import Path

import pytest
from predict_test_helpers import hand_event, predict_config, write_json

from buildlimits.predict.dataset import Snapshot, Source
from buildlimits.predict.events import EPOCH, Event
from buildlimits.predict.harness import walk_forward
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


def _backward() -> tuple[Event, Event]:
    """Return two verified events whose second was observed before the first.

    Each carries its own attempt identity: two rows sharing one would be a
    contradictory append, which the audit excludes before any clock question
    is asked.
    """
    return (
        dc.replace(
            hand_event(
                0,
                moments=(EPOCH, EPOCH + dt.timedelta(minutes=10)),
                label="hit",
            ),
            attempt="first",
        ),
        # Appended second, and admitted after the first, but its own reading
        # was stamped before the first one's: each admission still precedes its
        # own acceptance, so this is a clock step rather than a bad row.
        dc.replace(
            hand_event(
                1,
                moments=(
                    EPOCH + dt.timedelta(minutes=1),
                    EPOCH + dt.timedelta(minutes=5),
                ),
                label="hit",
            ),
            attempt="second",
        ),
    )


def _unverified() -> tuple[Event, Event]:
    """Return two rows written before availability was recorded.

    Their observation times run backwards, but neither carries a verified
    availability time, so there is no release-order question to answer.
    """
    return (
        hand_event(
            0,
            moments=(None, EPOCH + dt.timedelta(minutes=10)),
            label="hit",
            verified=False,
        ),
        hand_event(
            1,
            moments=(None, EPOCH + dt.timedelta(minutes=5)),
            label="hit",
            verified=False,
        ),
    )


def _forward() -> tuple[Event, Event]:
    """Return two events the store could have appended in this order."""
    return (
        dc.replace(hand_event(0, moments=(EPOCH, EPOCH), label="hit"), attempt="first"),
        dc.replace(
            hand_event(
                1,
                moments=(
                    EPOCH + dt.timedelta(minutes=5),
                    EPOCH + dt.timedelta(minutes=10),
                ),
                label="hit",
            ),
            attempt="second",
        ),
    )


def test_a_verified_history_with_a_backward_clock_is_refused(
    tmp_path: Path,
) -> None:
    """Across the step a label could sort before the prediction that used it."""
    document = snapshot_to_dict(_snapshot(*_backward()))

    with pytest.raises(ValueError, match="ran backwards"):
        load_snapshot(write_json(tmp_path / "history.jsonl", document))


def test_an_unverified_history_may_report_a_backward_clock(tmp_path: Path) -> None:
    """The step only matters where verified availability would release labels."""
    document = snapshot_to_dict(_snapshot(*_unverified()))

    loaded = load_snapshot(write_json(tmp_path / "history.jsonl", document))

    assert len(loaded.events) == 2, "the history is still readable"
    assert not any(event.label_observed_verified for event in loaded.events), (
        "no verified label time can be ordered by the broken clock"
    )


def test_a_backward_clock_is_refused_before_the_snapshot_is_written(
    tmp_path: Path,
) -> None:
    """A save cannot replace a good file with one its own reader refuses.

    The rule belongs to the value, not only to the reader: reporting success
    for a backward history would leave the dataset command's output unreadable
    and the earlier snapshot already gone. The refused save must therefore
    leave the file it would have replaced exactly as it was.
    """
    path = tmp_path / "history.jsonl"
    save_snapshot(path, _snapshot(*_forward()))
    earlier = path.read_bytes()

    with pytest.raises(ValueError, match="ran backwards"):
        save_snapshot(path, _snapshot(*_backward()))

    assert path.read_bytes() == earlier, "the earlier snapshot is left intact"


def test_rows_sharing_one_reading_load_and_score(tmp_path: Path) -> None:
    """Two launches finishing out of order share their reading's moment.

    The recorder dates every row of a window at the moment its statistics
    reading returned, so a launch that finished earlier carries the same stamp
    as the one appended before it. Store order is then monotonic even though
    the completions were reversed, and the history is scored rather than
    refused as a backwards clock.
    """
    label = EPOCH + dt.timedelta(minutes=10)
    shared = (
        hand_event(
            0,
            moments=(EPOCH, label),
            label="hit",
        ),
        hand_event(
            1,
            moments=(EPOCH + dt.timedelta(minutes=1), label),
            label="miss",
        ),
    )

    loaded = load_snapshot(
        write_json(tmp_path / "history.jsonl", snapshot_to_dict(_snapshot(*shared)))
    )
    walk = walk_forward(loaded.events, predict_config())

    assert len(loaded.events) == 2, "the shared moment is not a clock step"
    assert len(walk.moments) == 2, "both admission moments are predictable"
    assert all(phase is not None for phase in walk.phases), (
        "both labelled rows remain scorable"
    )
