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


def test_order_is_label_time_not_admission_time() -> None:
    """A slow compilation is predicted after earlier, quicker ones finish."""
    slow = _event(
        0,
        admitted_at=EPOCH,
        observed_at=EPOCH + dt.timedelta(minutes=10),
        label="hit",
    )
    quick = _event(
        1,
        admitted_at=EPOCH + dt.timedelta(minutes=1),
        observed_at=EPOCH + dt.timedelta(minutes=5),
        label="miss",
    )

    walk = walk_forward((slow, quick), load_config())

    assert walk.events == (quick, slow), "ordered by when the label arrived"


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

    walk = walk_forward((slow, middle), load_config())

    assert walk.series["last-outcome"].probabilities[0] is None, (
        "the intervening event has no earlier outcome for this identity"
    )
    assert walk.series["last-outcome"].probabilities[1] == pytest.approx(0.0), (
        "the middle event's miss is available once it has finished"
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


def test_label_order_differs_from_admission_order() -> None:
    """A stream ordered by label time is not ordered by admission time."""
    events = delayed(count=12, delay_steps=2)

    walk = walk_forward(events, load_config())

    assert [event.observed_at for event in walk.events] == sorted(
        event.observed_at for event in events
    ), "every event is predicted when its label arrives"
    admitted = sorted(
        events, key=lambda event: typ.cast("dt.datetime", event.admitted_at)
    )
    assert admitted != list(events), "admission order is not label order"
