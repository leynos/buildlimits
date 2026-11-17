"""Causality contracts: ordering, delayed labels and future-label leakage."""

import dataclasses as dc
import datetime as dt
import typing as typ

import pytest

from buildlimits.predict.config import load_config
from buildlimits.predict.events import EPOCH, Event
from buildlimits.predict.harness import walk_forward
from buildlimits.predict.synthetic import delayed, identity, stationary, with_unknowns

if typ.TYPE_CHECKING:
    from buildlimits.predict.events import Label


def _event(
    index: int,
    *,
    observed_at: dt.datetime,
    admitted_at: dt.datetime | None,
    label: Label,
) -> Event:
    """Return one event for a hand-built ordering scenario."""
    return Event(
        identity=identity(index),
        origin="git",
        admitted_at=admitted_at,
        observed_at=observed_at,
        label=label,
        runtime_seconds=1.0,
    )


def test_the_replay_keeps_store_order_when_timestamps_step_back() -> None:
    """A backward clock step never reorders records the store already wrote."""
    first = _event(
        0,
        admitted_at=EPOCH,
        observed_at=EPOCH + dt.timedelta(minutes=10),
        label="hit",
    )
    stepped = _event(
        1,
        admitted_at=EPOCH + dt.timedelta(minutes=1),
        observed_at=EPOCH + dt.timedelta(minutes=5),
        label="miss",
    )

    walk = walk_forward((first, stepped), load_config())

    assert walk.events == (first, stepped), "append order is kept"
    assert walk.batches == ((0,), (1,)), "each record is its own batch"


def test_labels_that_arrive_together_cannot_inform_each_other() -> None:
    """A batch is answered in full before any of it is learned."""
    stamp = EPOCH + dt.timedelta(minutes=5)
    first = _event(0, admitted_at=None, observed_at=stamp, label="hit")
    second = _event(0, admitted_at=None, observed_at=stamp, label="hit")
    later = _event(
        2, admitted_at=None, observed_at=stamp + dt.timedelta(minutes=1), label="miss"
    )
    latest = _event(
        3, admitted_at=None, observed_at=stamp + dt.timedelta(minutes=2), label="miss"
    )

    walk = walk_forward((first, second, later, latest), load_config())
    probabilities = walk.series["base-rate"].probabilities

    assert probabilities[:3] == (0.5, 0.5, 0.5), (
        "the tied pair is answered together and the next batch is embargoed"
    )
    assert probabilities[3] == pytest.approx(1.0), (
        "the tied hits are available two batches later"
    )


def test_the_replay_withholds_one_batch_and_counts_it() -> None:
    """Each batch is applied only after the next, and the last never is."""
    first = _event(0, admitted_at=None, observed_at=EPOCH, label="hit")
    second = _event(
        1, admitted_at=None, observed_at=EPOCH + dt.timedelta(minutes=1), label="miss"
    )
    third = _event(
        2, admitted_at=None, observed_at=EPOCH + dt.timedelta(minutes=2), label="hit"
    )

    walk = walk_forward((first, second, third), load_config())

    assert walk.batches == ((0,), (1,), (2,)), "one batch per label time"
    assert walk.embargoed_rows == 2, "the first two labels are each withheld once"
    assert walk.unapplied_rows == 1, "the final label informs no prediction"
    assert walk.series["base-rate"].probabilities == (0.5, 0.5, 1.0), (
        "only the first batch's label has reached the final prediction"
    )


def test_a_delayed_label_is_unavailable_to_an_intervening_prediction() -> None:
    """Work started earlier but finished later cannot inform the middle event."""
    slow = _event(
        7,
        admitted_at=EPOCH,
        observed_at=EPOCH + dt.timedelta(minutes=30),
        label="hit",
    )
    middle = _event(
        7,
        admitted_at=EPOCH + dt.timedelta(minutes=1),
        observed_at=EPOCH + dt.timedelta(minutes=10),
        label="miss",
    )
    later = _event(
        7,
        admitted_at=EPOCH + dt.timedelta(minutes=2),
        observed_at=EPOCH + dt.timedelta(minutes=50),
        label="hit",
    )

    walk = walk_forward((middle, slow, later), load_config())
    probabilities = walk.series["last-outcome"].probabilities

    assert probabilities[0] is None, "the middle event has no earlier outcome"
    assert probabilities[1] is None, (
        "the slow event is withheld from the batch after it"
    )
    assert probabilities[2] == pytest.approx(0.0), (
        "the middle event's miss is the only label applied so far"
    )


def test_flipping_a_future_label_cannot_change_an_earlier_prediction() -> None:
    """Every prediction at or before a label is unchanged when that label flips."""
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
    first = _event(0, admitted_at=None, observed_at=EPOCH, label="hit")
    second = _event(
        1, admitted_at=None, observed_at=EPOCH + dt.timedelta(minutes=1), label="miss"
    )
    tied = _event(
        2, admitted_at=None, observed_at=EPOCH + dt.timedelta(minutes=1), label="hit"
    )
    last = _event(
        3, admitted_at=None, observed_at=EPOCH + dt.timedelta(minutes=2), label="miss"
    )

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
