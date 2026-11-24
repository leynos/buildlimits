"""A label the source cannot date cannot leak into the replay.

A version 2 store row cannot prove which code stamped its observation time, so
the dataset adapter emits it unverified. The replay withholds an unverified
label: an admission that falls between a launch's completion and the statistics
query that labels it sees no label from that reading. A genuinely verified label
at the same moment *is* released to a later admission, so the exclusion is
measured rather than vacuous.
"""

import dataclasses as dc
import datetime as dt

from outcome_reading_test_helpers import (
    DELAYED_READING_AT,
    LAUNCH_COMPLETIONS,
    delayed_reading_rows,
    outcome_rows,
)
from outcome_test_helpers import OBSERVED_AT
from predict_test_helpers import hand_event, predict_config, record_events

from buildlimits.predict.harness import walk_forward


def test_a_row_written_between_completion_and_query_never_reveals_its_label() -> None:
    """A prediction admitted before the reading returned cannot learn its label.

    The store rows a delayed reading emits are dated at the reading, not at the
    launch's completion. The dataset adapter still marks them unverified,
    because a version 2 row cannot prove which code dated it. The replay must
    therefore withhold the label entirely: an admission between the completion
    and the query return sees no label from that reading, and a genuinely
    verified label at the same moment *is* released, so the exclusion is not
    vacuous.
    """
    records = delayed_reading_rows(
        completions=(OBSERVED_AT,), reading_at=DELAYED_READING_AT
    )
    rows = outcome_rows(records)
    assert len(rows) == 1, "the reading emitted one row"

    from_reading = record_events(records, now=DELAYED_READING_AT)
    # An admission that falls between the launch's completion and the query's
    # return. Its own label is deliberately unverified too, so only the two
    # withheld labels are in play and neither can be released to it.
    between = hand_event(
        9,
        admitted_at=LAUNCH_COMPLETIONS[1],
        observed_at=DELAYED_READING_AT,
        label="hit",
        verified=False,
    )
    # A later admission, so a label observed at the reading has a moment to be
    # released for. Without it the exclusion would be vacuously unobserved.
    after = hand_event(
        10,
        admitted_at=DELAYED_READING_AT + dt.timedelta(minutes=1),
        observed_at=DELAYED_READING_AT + dt.timedelta(minutes=2),
        label="miss",
    )
    stream = (*from_reading, between, after)
    walk = walk_forward(stream, predict_config())

    assert from_reading[0].label_observed_verified is False, (
        "a store row proves no label time, whatever code wrote it"
    )
    assert walk.unverified_label_time == 2, (
        "the store row and the unverified between-launch label are withheld"
    )
    assert walk.applied_labels == 0, (
        "the between admission sees no label from the reading that followed it"
    )

    verified = dc.replace(between, label_observed_verified=True)
    released = walk_forward((from_reading[0], verified, after), predict_config())

    assert released.unverified_label_time == 1, "only the store row is withheld"
    assert released.applied_labels == 1, (
        "a genuinely verified label at the same moment is released to the later "
        "admission, so the exclusion is measured rather than vacuous"
    )
