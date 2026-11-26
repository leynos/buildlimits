"""The independent-label gate and the population it reports.

Two questions are answered here. Which rows may count toward the minimum-label
gate, where only a complete attempt pair that no correlation exclusion rejects
is independent evidence? And what does the published ``uncorrelated`` count
mean, given that it exists to say how much of a history rests on independence a
repeated append cannot check?

The rows that fail these questions are not discarded: a legacy row still
replays and scores. It simply does not claim to be independent evidence, and
the count names only the labelled, verified rows that justify it.
"""

import dataclasses as dc
import datetime as dt

from predict_test_helpers import hand_event, predict_config
from predict_timing_helpers import event

from buildlimits.predict.evaluation import evaluate
from buildlimits.predict.events import EPOCH
from buildlimits.predict.harness import walk_forward
from buildlimits.predict.sufficiency import STATUS_INSUFFICIENT
from buildlimits.predict.timing import audit


def test_repeated_attempts_do_not_satisfy_the_label_gate() -> None:
    """A label gate counts independent invocations, not repeated appends.

    Forty attempts appended twice each give eighty labelled rows but only forty
    independent ones. Counting the raw rows would let a history that is mostly
    retries clear a gate of fifty and reach a verdict on evidence it does not
    have.
    """
    events = tuple(
        dc.replace(
            hand_event(
                attempt,
                moments=(
                    EPOCH + dt.timedelta(seconds=attempt),
                    EPOCH + dt.timedelta(seconds=attempt + 1),
                ),
                label="hit" if attempt % 2 else "miss",
            ),
            attempt=f"g{attempt}",
        )
        for attempt in range(40)
        for _copy in range(2)
    )
    config = dc.replace(predict_config(), minimum_labelled_events=50)

    walk = walk_forward(events, config)

    assert walk.labelled == 80, "every row carries a label"
    assert walk.usable_labelled == 40, "only the distinct attempts are evidence"
    assert evaluate(walk, config).status == STATUS_INSUFFICIENT, (
        "the gate counts independent labels, so the retries cannot clear it"
    )


def test_a_legacy_row_replays_but_does_not_claim_independence() -> None:
    """A row with no attempt pair is evidence of a label, not of independence.

    The compatibility rule keeps such a row scorable: it proves when its label
    was accepted, which is the question the replay asks. It cannot prove that
    it is a distinct invocation, so it does not count toward a gate whose
    purpose is to require independent labels. Otherwise a legacy history could
    satisfy the gate with rows whose duplication the document cannot rule out.
    """
    legacy = tuple(
        dc.replace(
            hand_event(
                index,
                moments=(
                    EPOCH + dt.timedelta(seconds=index),
                    EPOCH + dt.timedelta(seconds=index + 1),
                ),
                label="hit",
            ),
            attempt=None,
            supervisor_generation=None,
        )
        for index in range(12)
    )
    config = dc.replace(predict_config(), minimum_labelled_events=10)

    walk = walk_forward(legacy, config)

    assert walk.labelled == 12, "every legacy row carries a label"
    assert walk.usable_labelled == 0, "none of them proves its independence"
    assert sum(len(group) for group in walk.moments) == 12, "each row still replays"
    assert evaluate(walk, config).status == STATUS_INSUFFICIENT, (
        "the gate is not cleared by rows whose independence is unknown"
    )


def test_an_uncorrelated_count_states_only_the_labels_it_means() -> None:
    """An unknown outcome is not a label, so it cannot inflate the count.

    The count exists to say how much of the label gate rests on independence
    that a repeated append cannot check. A row with no hit or miss outcome
    carries no label at all, and an unverified row has no moment to place, so
    neither belongs in that population.
    """
    legacy = dc.replace(
        event(0, moments=(1.0, 5.0)),
        attempt=None,
        supervisor_generation=None,
    )
    unknown = dc.replace(
        event(1, moments=(2.0, 6.0), label="unknown"),
        attempt=None,
        supervisor_generation=None,
    )

    verdict = audit((legacy, unknown))

    assert verdict.uncorrelated == 1, "only the labelled legacy row is counted"


def test_a_labelled_unverified_row_is_not_counted_as_uncorrelated() -> None:
    """A row that cannot prove its time is withheld, not uncorrelated.

    It is already reported through the availability gate. Counting it here as
    well would state the same absence twice and overstate the population whose
    independence is merely unknown.
    """
    withheld = dc.replace(
        event(0, moments=(1.0, 5.0)),
        label_observed_verified=False,
        attempt=None,
        supervisor_generation=None,
    )

    verdict = audit((withheld,))

    assert verdict.uncorrelated == 0, "a withheld label is not counted here"
    assert verdict.reasons == ("no_availability_time",), (
        "the availability gate reports it instead"
    )
