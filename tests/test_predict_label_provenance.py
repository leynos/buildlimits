"""A label's time is scored only when the store row proves it.

A version 1 or 2 store row cannot prove which code stamped its observation
time, so the dataset adapter emits it unverified and the replay withholds it.
The fixed recorder's version 3 row states that its time is proven, so the same
reading round-trips as verified and is scored. Both directions are measured
here: an unverified label at a moment is withheld, a verified label at the
same moment is released, and the two histories differ only in what the row
itself states.
"""

import dataclasses as dc
import datetime as dt
import typing as typ

from outcome_reading_test_helpers import (
    DELAYED_READING_AT,
    LAUNCH_COMPLETIONS,
    delayed_reading_rows,
    outcome_rows,
)
from outcome_test_helpers import OBSERVED_AT
from predict_test_helpers import hand_event, predict_config, record_events

from buildlimits.predict.harness import walk_forward

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.predict.events import Event
    from buildlimits.predict.harness import Walk


def _walk_around_reading(
    records: tuple[StoreRecord, ...], *, version: int
) -> tuple[tuple[Event, ...], Walk]:
    """Walk one reading's history around the moment its label became known.

    The stream holds the reading's own rows, then an event admitted between the
    launch's completion and the query's return, then a later admission the
    released label could inform. Without the later admission the exclusion
    could not be observed, so it is always present.

    Returns
    -------
    tuple[tuple[Event, ...], Walk]
        The loaded events and the walk-forward result over them.
    """
    from_reading = record_events(records, now=DELAYED_READING_AT, version=version)
    between = hand_event(
        9,
        admitted_at=LAUNCH_COMPLETIONS[1],
        observed_at=DELAYED_READING_AT,
        label="hit",
        verified=False,
    )
    after = hand_event(
        10,
        admitted_at=DELAYED_READING_AT + dt.timedelta(minutes=1),
        observed_at=DELAYED_READING_AT + dt.timedelta(minutes=2),
        label="miss",
    )
    stream = (*from_reading, between, after)
    return from_reading, walk_forward(stream, predict_config())


def test_a_version_two_row_written_between_completion_and_query_stays_withheld() -> (
    None
):
    """A version 2 row proves no label time, so its label never leaks.

    The rows a delayed reading emits are dated at the reading, not at the
    launch's completion. A version 2 document cannot carry the marker that
    says so, so the adapter emits them unverified and an admission between the
    completion and the query return sees no label from that reading.
    """
    records = delayed_reading_rows(
        completions=(OBSERVED_AT,), reading_at=DELAYED_READING_AT
    )
    rows = outcome_rows(records)
    assert len(rows) == 1, "the reading emitted one row"

    from_reading, walk = _walk_around_reading(records, version=2)

    assert from_reading[0].label_observed_verified is False, (
        "a version 2 row proves no label time"
    )
    assert walk.unverified_label_time == 2, (
        "the store row and the unverified between-launch label are withheld"
    )
    assert walk.applied_labels == 0, (
        "the between admission sees no label from the reading that followed it"
    )


def test_a_version_three_row_round_trips_as_verified_and_is_scored() -> None:
    """The fixed recorder's own row proves its label time and is scored.

    The same delayed reading is written as the current document the recorder
    now emits. Its row states ``verified``, so the dataset command loads the
    label as proven and the later admission is informed by it. That is the one
    behaviour a version 2 row could never produce, so the marker is doing real
    work rather than restating the default.
    """
    records = delayed_reading_rows(
        completions=(OBSERVED_AT,), reading_at=DELAYED_READING_AT
    )

    from_reading, walk = _walk_around_reading(records, version=3)

    assert from_reading[0].label_observed_verified is True, (
        "the current recorder dates the row at the reading, and says so"
    )
    assert walk.unverified_label_time == 1, (
        "only the deliberately unverified between-launch label is withheld"
    )
    assert walk.applied_labels == 1, (
        "the verified reading label is released to the later admission"
    )


def test_an_unverified_label_at_the_same_moment_is_never_released() -> None:
    """Withholding follows the marker, not the timestamp.

    A label observed at exactly the moment a verified one is released is not
    released when its own row could not prove the time, so the exclusion is
    measured rather than vacuous.
    """
    records = delayed_reading_rows(
        completions=(OBSERVED_AT,), reading_at=DELAYED_READING_AT
    )
    from_reading = record_events(records, now=DELAYED_READING_AT, version=2)
    after = hand_event(
        10,
        admitted_at=DELAYED_READING_AT + dt.timedelta(minutes=1),
        observed_at=DELAYED_READING_AT + dt.timedelta(minutes=2),
        label="miss",
    )

    withheld = walk_forward((from_reading[0], after), predict_config())
    released = walk_forward(
        (dc.replace(from_reading[0], label_observed_verified=True), after),
        predict_config(),
    )

    assert withheld.applied_labels == 0, "the unproven label is withheld"
    assert released.applied_labels == 1, (
        "the same moment, once proven, is released to the later admission"
    )
