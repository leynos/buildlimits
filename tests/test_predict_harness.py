"""Causality contracts: admission order, delayed labels and future-label leakage."""

import dataclasses as dc
import datetime as dt
import itertools
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from predict_test_helpers import hand_event

from buildlimits.predict.config_loader import load_config
from buildlimits.predict.events import EPOCH, Event
from buildlimits.predict.harness import walk_forward
from buildlimits.predict.scoring import rows_for
from buildlimits.predict.synthetic import (
    STEP,
    delayed,
    stationary,
    with_unknowns,
)

if typ.TYPE_CHECKING:
    from buildlimits.predict.events import Label

MINUTE = dt.timedelta(minutes=1)


def _causal(
    index: int, *, observed_at: dt.datetime, label: Label, early: dt.timedelta = MINUTE
) -> Event:
    """Return one event admitted ``early`` before its own label."""
    return hand_event(index, moments=(observed_at - early, observed_at), label=label)


def test_the_replay_keeps_store_order_when_timestamps_step_back() -> None:
    """A backward clock step never reorders the records the store wrote."""
    first = hand_event(0, moments=(EPOCH, EPOCH + 10 * MINUTE), label="hit")
    stepped = hand_event(1, moments=(EPOCH + MINUTE, EPOCH + 5 * MINUTE), label="miss")

    walk = walk_forward((first, stepped), load_config())

    assert walk.events == (first, stepped), "append order is kept"
    assert walk.moments == ((0,), (1,)), "admissions ascend even as labels step back"


def test_predictions_follow_admission_order_not_store_order() -> None:
    """The event admitted first is answered first, whatever the store order."""
    later = hand_event(
        5, moments=(EPOCH + 10 * MINUTE, EPOCH + 20 * MINUTE), label="hit"
    )
    earlier = hand_event(6, moments=(EPOCH, EPOCH + 2 * MINUTE), label="miss")

    walk = walk_forward((later, earlier), load_config())
    probabilities = walk.series["base-rate"].probabilities

    assert probabilities[1] == pytest.approx(0.5), "the first admission has no history"
    assert probabilities[0] == pytest.approx(0.0), (
        "the later admission sees the earlier event's miss"
    )


def test_labels_observed_with_an_admission_cannot_inform_it() -> None:
    """A row is answered before any label from its own moment is released."""
    stamp = EPOCH + 5 * MINUTE
    first = dc.replace(
        hand_event(0, moments=(stamp, stamp), label="hit"), attempt="g-first"
    )
    second = dc.replace(
        hand_event(0, moments=(stamp, stamp), label="hit"), attempt="g-second"
    )
    later = _causal(2, observed_at=stamp + 10 * MINUTE, label="miss", early=MINUTE)

    walk = walk_forward((first, second, later), load_config())
    probabilities = walk.series["base-rate"].probabilities

    assert walk.moments == ((0, 1), (2,)), "the tied pair shares one moment"
    assert probabilities[:2] == (0.5, 0.5), "neither tied row sees the other's label"
    assert probabilities[2] == pytest.approx(1.0), (
        "both hits are released before the later admission"
    )


def test_the_replay_counts_the_labels_it_delivers_and_the_ones_it_cannot() -> None:
    """A label informs a prediction only when it arrives strictly earlier."""
    first = _causal(0, observed_at=EPOCH, label="hit")
    second = hand_event(1, moments=(EPOCH, EPOCH), label="hit")
    third = _causal(2, observed_at=EPOCH + 5 * MINUTE, label="miss", early=MINUTE)

    walk = walk_forward((first, second, third), load_config())

    assert walk.moments == ((0,), (1,), (2,)), "one moment per admission"
    assert walk.applied_labels == 2, "both tied hits inform the later prediction"
    assert walk.unapplied_labels == 1, "the final label informs no prediction"
    assert walk.series["base-rate"].probabilities == (0.5, 0.5, 1.0), (
        "the tied hit is not released before its own moment"
    )


def test_a_delayed_label_is_unavailable_to_an_intervening_prediction() -> None:
    """The reviewer's case: a label observed at minute 10 is not had at minute 2."""
    slow = hand_event(7, moments=(EPOCH, EPOCH + 30 * MINUTE), label="hit")
    middle = hand_event(7, moments=(EPOCH + MINUTE, EPOCH + 10 * MINUTE), label="miss")
    at_two = hand_event(
        7, moments=(EPOCH + 2 * MINUTE, EPOCH + 50 * MINUTE), label="hit"
    )
    after = hand_event(
        7, moments=(EPOCH + 31 * MINUTE, EPOCH + 60 * MINUTE), label="hit"
    )

    walk = walk_forward((middle, slow, at_two, after), load_config())
    probabilities = walk.series["last-outcome"].probabilities

    assert probabilities[0] is None, "the minute-1 admission has no earlier outcome"
    assert probabilities[1] is None, "the minute-0 admission has no earlier outcome"
    assert probabilities[2] is None, (
        "the minute-2 admission must not use the miss observed at minute 10"
    )
    assert probabilities[3] == pytest.approx(1.0), (
        "both delayed labels are released before minute 31, newest first"
    )


def test_an_event_without_an_admission_time_is_never_predicted() -> None:
    """A missing timestamp is not evidence that the label arrived early."""
    known = _causal(0, observed_at=EPOCH, label="hit")
    missing = hand_event(1, moments=(None, EPOCH + MINUTE), label="miss")

    walk = walk_forward((known, missing), load_config())

    assert walk.rows[1] is None, "no feature row exists without an admission time"
    assert walk.series["base-rate"].probabilities[1] is None, "nothing is predicted"
    assert walk.unavailable_admission == 1, "the event is reported as unavailable"
    assert walk.timing.reasons[1] == "no_admission_time", (
        "the exclusion names the missing decision time"
    )
    assert walk.phases[1] is None, (
        "an unverified label is never placed in a period or scored"
    )


def test_an_admission_recorded_after_its_label_is_unusable() -> None:
    """A decision taken once the outcome was known cannot be predicted."""
    event = hand_event(0, moments=(EPOCH + 5 * MINUTE, EPOCH), label="hit")

    walk = walk_forward((event,), load_config())

    assert walk.rows[0] is None, "the admission cannot support a prediction"
    assert walk.unavailable_admission == 1, "the retroactive time is counted as missing"
    assert walk.unapplied_labels == 0, "its label never trains anything either"


def test_a_label_from_an_unpredicted_event_never_trains_a_predictor() -> None:
    """Removing the timestamp of a labelled event drops it from the history."""
    events = tuple(
        _causal(
            index,
            observed_at=EPOCH + index * MINUTE,
            label="hit" if index % 2 else "miss",
        )
        for index in range(6)
    )
    hidden = tuple(
        dc.replace(event, admitted_at=None) if index == 1 else event
        for index, event in enumerate(events)
    )

    original = walk_forward(events, load_config())
    without = walk_forward(hidden, load_config())

    assert without.applied_labels == original.applied_labels - 1, (
        "the hidden event's label is dropped from the training history"
    )
    assert without.rows[3] is not None and original.rows[3] is not None, (
        "the fourth event is predicted on both walks"
    )
    assert without.rows[3].load == original.rows[3].load - 1, (
        "the hidden label is not counted as earlier load"
    )
    assert without.series["base-rate"].probabilities[3] == pytest.approx(0.0), (
        "only the hidden event's hit could have raised the rate"
    )
    assert original.series["base-rate"].probabilities[3] == pytest.approx(0.5), (
        "the unhidden history holds one hit and one miss"
    )


def test_flipping_a_future_label_cannot_change_an_earlier_prediction() -> None:
    """Every prediction before a label is unchanged when that label flips."""
    config = load_config()
    events = stationary(count=300, rate=0.6, seed=21)
    cut = 150
    flipped = tuple(
        dc.replace(event, label="miss" if event.label == "hit" else "hit")
        if index >= cut
        else event
        for index, event in enumerate(events)
    )

    original = walk_forward(events, config)
    mutated = walk_forward(flipped, config)

    for name, series in original.series.items():
        assert (
            series.probabilities[: cut + 1]
            == (mutated.series[name].probabilities[: cut + 1])
        ), f"{name} read a label it could not have had"


def test_mutating_one_label_leaves_earlier_features_identical() -> None:
    """Causal features depend on earlier events only, never on the label."""
    config = load_config()
    events = stationary(count=200, rate=0.5, seed=31)
    flipped = tuple(
        dc.replace(event, label="unknown") if index == 120 else event
        for index, event in enumerate(events)
    )

    original = walk_forward(events, config)
    mutated = walk_forward(flipped, config)

    assert original.rows[:121] == mutated.rows[:121], "features are label-free"


def test_split_is_the_configured_share_of_labelled_events() -> None:
    """The split counts labelled events on each side of the boundary."""
    config = dc.replace(load_config(), train_fraction=0.5)
    events = stationary(count=200, rate=0.5, seed=12)

    walk = walk_forward(events, config)

    assert walk.split.train_labelled == 100, "half the labels tune"
    assert walk.split.heldout_labelled == 100, "half the labels are held out"
    assert walk.split.boundary == events[100].observed_at, "boundary timestamp"


def test_a_batch_stays_inside_one_phase() -> None:
    """The tuning/held-out boundary never splits a batch."""
    config = dc.replace(load_config(), train_fraction=0.5)
    first = _causal(0, observed_at=EPOCH, label="hit")
    second = _causal(1, observed_at=EPOCH + MINUTE, label="miss")
    tied = _causal(2, observed_at=EPOCH + MINUTE, label="hit")
    last = _causal(3, observed_at=EPOCH + 2 * MINUTE, label="miss")

    walk = walk_forward((first, second, tied, last), config)

    assert walk.phases == ("train", "train", "train", "held-out"), (
        "the tie-reached batch tunes whole"
    )
    assert walk.split.train_labelled == 3, "the batch that reaches the target tunes"
    assert walk.split.heldout_labelled == 1, "the last batch is held out"


def test_unknown_events_are_predicted_but_never_phase_labelled() -> None:
    """An unknown outcome is still predicted, yet carries no phase."""
    events = with_unknowns(count=80)

    walk = walk_forward(events, load_config())

    assert len(walk.phases) == len(walk.events), "one phase slot per event"
    assert walk.phases[0] is None, "unknown rows have no phase"
    assert sum(1 for phase in walk.phases if phase is None) == 27, (
        "every third row is unknown"
    )
    assert all(
        probability is not None
        for probability in walk.series["base-rate"].probabilities
    ), "the base rate still answers unknown rows"


def test_the_replay_keeps_store_order_not_admission_time() -> None:
    """Staggered admissions do not reorder the records the store wrote."""
    events = delayed(count=12, delay_steps=2)

    walk = walk_forward(events, load_config())

    assert walk.events == events, "records are never re-sorted"
    admitted = sorted(
        events, key=lambda event: typ.cast("dt.datetime", event.admitted_at)
    )
    assert admitted != list(events), "admission order is not write order"
    order = [index for group in walk.moments for index in group]
    assert order != list(range(12)), "the moments follow admission, not store order"


_OPPOSITE: dict[str, Label] = {"hit": "miss", "miss": "hit", "unknown": "unknown"}


@given(
    st.integers(min_value=8, max_value=40),
    st.floats(min_value=0.05, max_value=0.95),
    st.floats(min_value=0.05, max_value=0.95),
    st.integers(min_value=0, max_value=1_000),
)
@settings(max_examples=50, derandomize=True, deadline=None)
def test_flipping_a_later_label_never_changes_an_earlier_answer(
    count: int, cut_share: float, rate: float, seed: int
) -> None:
    """A label that arrives after a prediction cannot be part of its answer.

    Flipping the labels from the cut onwards must leave every answer for an
    event admitted before that cut byte-identical, whichever predictor gave it.
    """
    events = stationary(count=count, rate=rate, seed=seed)
    cut = max(1, int(count * cut_share))
    flipped = tuple(
        dc.replace(event, label=_OPPOSITE[event.label]) if index >= cut else event
        for index, event in enumerate(events)
    )

    before = walk_forward(events, load_config())
    after = walk_forward(flipped, load_config())
    # The earliest flipped label arrives at events[cut].observed_at, so a
    # prediction made at or before that instant cannot have read it.
    protected = [
        index
        for index, event in enumerate(events)
        if typ.cast("dt.datetime", event.admitted_at) <= events[cut].observed_at
    ]

    assert flipped != events, "the perturbation changed at least one label"
    for name, series in before.series.items():
        assert [series.probabilities[index] for index in protected] == [
            after.series[name].probabilities[index] for index in protected
        ], f"{name} read a label that had not arrived"


@given(st.lists(st.sampled_from(("hit", "miss", "unknown")), min_size=3, max_size=30))
@settings(max_examples=50, derandomize=True, deadline=None)
def test_an_unknown_outcome_never_becomes_a_scored_miss(labels: list[Label]) -> None:
    """An unattributable outcome is predicted but contributes no scored row."""
    events = tuple(
        _causal(index, observed_at=EPOCH + index * STEP, label=label)
        for index, label in enumerate(labels)
    )

    walk = walk_forward(events, load_config())
    rows = rows_for(walk, "base-rate", walk.series["base-rate"].probabilities)

    assert len(rows) == sum(label != "unknown" for label in labels), (
        "every hit and miss is scored, and nothing else"
    )
    assert sum(1 for row in rows if row.outcome == 0) == labels.count("miss"), (
        "an unknown outcome adds no miss"
    )


def test_the_walk_records_what_the_injected_clock_reported() -> None:
    """A stub clock makes every recorded latency exactly the interval it gave."""
    step = 0.25
    ticks = itertools.count(0.0, step)
    events = tuple(
        _causal(index, observed_at=EPOCH + index * STEP, label="hit")
        for index in range(3)
    )

    walk = walk_forward(events, load_config(), timer=lambda: next(ticks))

    for name, series in walk.series.items():
        assert series.elapsed == pytest.approx((step,) * 3), (
            f"{name} timed its predictions with the supplied clock"
        )
