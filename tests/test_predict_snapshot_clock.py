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


def _load(tmp_path: Path, *events: Event) -> Snapshot:
    """Write ``events`` as a snapshot document and read it back.

    Returns
    -------
    Snapshot
        The history the reader accepted.
    """
    document = snapshot_to_dict(_snapshot(*events))
    return load_snapshot(write_json(tmp_path / "history.jsonl", document))


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
    with pytest.raises(ValueError, match="ran backwards"):
        _load(tmp_path, *_backward())


def _pair(
    first: tuple[dt.datetime, dt.datetime],
    second: tuple[dt.datetime, dt.datetime],
) -> tuple[Event, Event]:
    """Return two events whose moments are supplied as (decision, label) pairs.

    Returns
    -------
    tuple[Event, Event]
        The two verified events, in append order.
    """
    return (
        hand_event(0, moments=first, label="hit"),
        hand_event(1, moments=second, label="miss"),
    )


def test_a_label_available_before_an_earlier_decision_is_refused(
    tmp_path: Path,
) -> None:
    """A step that leaves label times increasing can still cross a decision.

    The first row proves its label at five. The second is appended between the
    labels and records a decision at thirty, but withholds its own label. The
    third proves its label at twenty. The verified label times a clock can
    order are therefore five then twenty — increasing, so the availability
    rule sees nothing — yet the later label precedes a decision the file
    already recorded, which no monotonic clock can produce. Only the
    decision-crossing rule catches this history.
    """
    first = hand_event(0, moments=(EPOCH, EPOCH + dt.timedelta(seconds=5)), label="hit")
    between = hand_event(
        1,
        moments=(EPOCH + dt.timedelta(seconds=30), EPOCH),
        label="miss",
        verified=False,
    )
    third = hand_event(
        2,
        moments=(
            EPOCH + dt.timedelta(seconds=10),
            EPOCH + dt.timedelta(seconds=20),
        ),
        label="miss",
    )

    with pytest.raises(ValueError, match="cannot precede a decision"):
        _load(tmp_path, first, between, third)


def test_a_slow_build_appended_after_a_later_decision_is_allowed(
    tmp_path: Path,
) -> None:
    """Append order is reading order, so an earlier decision may still follow.

    The first row is a fast build decided at one and labelled at five. The
    second is a slow build decided at three and labelled at twenty. Its
    decision precedes its own label and follows nothing later than itself, so
    the history is ordered.
    """
    loaded = _load(
        tmp_path,
        *_pair(
            (EPOCH + dt.timedelta(seconds=1), EPOCH + dt.timedelta(seconds=5)),
            (EPOCH + dt.timedelta(seconds=3), EPOCH + dt.timedelta(seconds=20)),
        ),
    )

    assert len(loaded.events) == 2, "both readings are ordered by their own moments"


def test_a_restart_does_not_reset_the_clock_it_must_order(tmp_path: Path) -> None:
    """A new supervisor generation appends after the file, not after zero.

    Both rows prove their label times, so the availability rule applies, and
    they were written by different supervisor generations. The check is a
    property of the file, not of either process: the second generation's
    earlier stamp is still a step, so it is refused rather than excused as a
    fresh clock domain.
    """
    first = dc.replace(
        hand_event(0, moments=(EPOCH, EPOCH + dt.timedelta(seconds=10)), label="hit"),
        supervisor_generation="generation-one",
    )
    second = dc.replace(
        hand_event(1, moments=(EPOCH, EPOCH + dt.timedelta(seconds=5)), label="hit"),
        supervisor_generation="generation-two",
    )

    with pytest.raises(ValueError, match="ran backwards"):
        _load(tmp_path, first, second)


def test_a_delayed_reading_lands_after_the_rows_already_written(
    tmp_path: Path,
) -> None:
    """A late delivery is stamped late, so it cannot cross an earlier decision.

    The first row is a reading taken at ten for a build decided at five. The
    second row's build was decided at zero, but its statistics were only
    delivered at thirty, so it was appended afterwards and stamped then. Its
    decision precedes the first row's, which is harmless: the rule compares a
    label with decisions recorded before it, and the only such decision is
    later than nothing. The history loads.
    """
    loaded = _load(
        tmp_path,
        *_pair(
            (EPOCH + dt.timedelta(seconds=5), EPOCH + dt.timedelta(seconds=10)),
            (EPOCH, EPOCH + dt.timedelta(seconds=30)),
        ),
    )

    assert len(loaded.events) == 2, "acceptance order, not decision order, is stored"


def test_an_unverified_history_may_report_a_backward_clock(tmp_path: Path) -> None:
    """The step only matters where verified availability would release labels."""
    loaded = _load(tmp_path, *_unverified())

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

    loaded = _load(tmp_path, *shared)
    walk = walk_forward(loaded.events, predict_config())

    assert len(loaded.events) == 2, "the shared moment is not a clock step"
    assert len(walk.moments) == 2, "both admission moments are predictable"
    assert all(phase is not None for phase in walk.phases), (
        "both labelled rows remain scorable"
    )
