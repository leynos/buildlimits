"""The prequential walk over one event stream, ordered by admission time.

Every prediction is made at its event's admission moment, and only labels
verified strictly earlier are delivered first. An event without a usable
admission time is never predicted and never trains a predictor: a missing
timestamp is not evidence that the label arrived before the decision. Store
order breaks ties, so nothing is re-sorted behind the store's back.
"""

import dataclasses as dc
import time
import typing as typ

from buildlimits.predict.events import Features, scored_label
from buildlimits.predict.history import (
    admission_schedule,
    features,
    prediction_moments,
    released_labels,
    unverified_label_count,
)
from buildlimits.predict.phases import Split, phases, split
from buildlimits.predict.populations import population
from buildlimits.predict.predictors import single_predictors, update_all
from buildlimits.predict.timing import TimingAudit, audit

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.predict.config import Config
    from buildlimits.predict.events import Event
    from buildlimits.predict.metrics import Phase
    from buildlimits.predict.predictors import Predictor


@dc.dataclass(frozen=True, slots=True)
class Series:
    """One predictor's answers and the wall time each one took.

    Attributes
    ----------
    name : str
        The predictor's stable name.
    probabilities : tuple[float | None, ...]
        One answer per event, in store order; None where no prediction was
        possible or the predictor abstained.
    elapsed : tuple[float, ...]
        Seconds spent predicting each event.
    """

    name: str
    probabilities: tuple[float | None, ...]
    elapsed: tuple[float, ...]


@dc.dataclass(frozen=True, slots=True)
class Walk:
    """The ordered stream with every single predictor's answers.

    Attributes
    ----------
    events : tuple[Event, ...]
        The stream, in the store's append order.
    rows : tuple[Features | None, ...]
        Causal features aligned with ``events``; None where no prediction is
        possible.
    phases : tuple[Phase | None, ...]
        The phase of each labelled event, or None for an unscored one.
    series : dict[str, Series]
        Answers per single predictor, in creation order.
    split : Split
        The tuning/held-out boundary.
    moments : tuple[tuple[int, ...], ...]
        Event indices grouped by equal admission time, in time order.
    timing : TimingAudit
        Per-event usability against the timing, label and correlation gates,
        with the closed exclusion reasons a report counts.
    labelled : int
        Events carrying a hit or miss label, whether or not their timing was
        verified. This is the label-coverage denominator, kept apart from the
        phase counts so a honeypot of unverified labels cannot hide behind
        ``insufficient data``.
    independent_labelled : int
        Labelled events that carry a complete attempt pair and no correlation
        exclusion. This is the independence count a report states beside the
        gate's population: a repeated or contradictory append cannot inflate
        it, and a row whose independence cannot be established does not claim
        to be independent evidence.
    usable_labelled : int
        The independent labels whose timing the audit also accepts. This is
        the population the minimum-label gate counts, so a history cannot clear
        it with labels that are independent but unverified, or verified but
        of unknown independence. A legacy row still replays and scores; it
        simply does not count here.
    admission_usable_labelled : int
        The independent labels whose decision time is usable. Compared with
        ``availability_usable_labelled``, it names which half of the timing
        evidence a failing gate is actually short of.
    availability_usable_labelled : int
        The independent labels whose availability time is usable.
    unavailable_admission : int
        Events that carry no usable admission time and were never predicted.
    unavailable_availability : int
        Admitted events with no verified availability. A row excluded for
        another reason is counted by that reason instead.
    applied_labels : int
        Eligible labels delivered before at least one prediction.
    unapplied_labels : int
        Eligible labels that arrived too late to inform any prediction.
    unverified_label_time : int
        Hit or miss labels withheld: their recorded time is unproven.
    """

    events: tuple[Event, ...]
    rows: tuple[Features | None, ...]
    phases: tuple[Phase | None, ...]
    series: dict[str, Series]
    split: Split
    moments: tuple[tuple[int, ...], ...]
    timing: TimingAudit
    labelled: int
    independent_labelled: int
    usable_labelled: int
    admission_usable_labelled: int
    availability_usable_labelled: int
    unavailable_admission: int
    unavailable_availability: int
    applied_labels: int
    unapplied_labels: int
    unverified_label_time: int

    def components(self) -> tuple[str, ...]:
        """Return the single predictor names in their fixed order.

        Returns
        -------
        tuple[str, ...]
            Component names.
        """
        return tuple(self.series)


def walk_forward(
    events: cabc.Iterable[Event],
    config: Config,
    *,
    timer: cabc.Callable[[], float] = time.perf_counter,
) -> Walk:
    """Predict every event at its admission, updating from released labels.

    Parameters
    ----------
    events : Iterable[Event]
        The recorded stream, in store append order.
    config : Config
        The pinned parameters, including the load window and split fraction.
    timer : Callable[[], float]
        Monotonic seconds, injected so a test can record known timings.

    Returns
    -------
    Walk
        The stream, its features, phases and each single predictor's answers.
    """
    stream = tuple(events)
    timing = audit(stream)
    moments = prediction_moments(stream, usable=timing.usable)
    rows = features(
        stream,
        load_window_seconds=config.load_window_seconds,
        usable=timing.usable,
    )
    assigned = phases(stream, timing, train_fraction=config.train_fraction)
    replay = _replay(
        single_predictors(config),
        rows,
        _Stream(stream, moments),
        timer=timer,
    )
    series = {
        name: Series(
            name, tuple(replay.probabilities[name]), tuple(replay.elapsed[name])
        )
        for name in replay.probabilities
    }
    counts = population(stream, timing)
    return Walk(
        events=stream,
        rows=rows,
        phases=assigned,
        series=series,
        split=split(stream, assigned),
        moments=moments,
        timing=timing,
        labelled=counts.labelled,
        independent_labelled=counts.independent_labelled,
        usable_labelled=counts.usable_labelled,
        admission_usable_labelled=counts.admission_usable_labelled,
        availability_usable_labelled=counts.availability_usable_labelled,
        unavailable_admission=counts.unavailable_admission,
        unavailable_availability=counts.unavailable_availability,
        applied_labels=replay.applied_labels,
        unapplied_labels=replay.unapplied_labels,
        unverified_label_time=unverified_label_count(stream),
    )


@dc.dataclass(frozen=True, slots=True)
class _Replay:
    """One admission-scheduled pass over the stream.

    Attributes
    ----------
    probabilities : dict[str, list[float | None]]
        Answers per predictor, in store order.
    elapsed : dict[str, list[float]]
        Seconds spent predicting each event, per predictor.
    applied_labels : int
        Eligible labels delivered before at least one prediction.
    unapplied_labels : int
        Eligible labels that no prediction followed.
    """

    probabilities: dict[str, list[float | None]]
    elapsed: dict[str, list[float]]
    applied_labels: int
    unapplied_labels: int


@dc.dataclass(frozen=True, slots=True)
class _Stream:
    """The two orderings a replay answers in.

    Attributes
    ----------
    events : tuple[Event, ...]
        The stream in store append order.
    moments : tuple[tuple[int, ...], ...]
        The admission moments, earliest first, each holding its stream indices.
    """

    events: tuple[Event, ...]
    moments: tuple[tuple[int, ...], ...]


@dc.dataclass(slots=True)
class _Answers:
    """The per-predictor columns a replay is filling in.

    Attributes
    ----------
    probabilities : dict[str, list[float | None]]
        Answers per predictor, in store order.
    elapsed : dict[str, list[float]]
        Seconds spent predicting each event, per predictor.
    timer : Callable[[], float]
        Monotonic seconds, injected rather than read from the wall clock.
    """

    probabilities: dict[str, list[float | None]]
    elapsed: dict[str, list[float]]
    timer: cabc.Callable[[], float]


def _replay(
    predictors: cabc.Sequence[Predictor],
    rows: cabc.Sequence[Features | None],
    stream: _Stream,
    *,
    timer: cabc.Callable[[], float],
) -> _Replay:
    """Answer every admission moment, releasing only earlier labels first.

    Returns
    -------
    _Replay
        The answers, timings and the label accounting.
    """
    events = stream.events
    answers = _Answers(
        probabilities={
            predictor.name: [None] * len(events) for predictor in predictors
        },
        elapsed={predictor.name: [0.0] * len(events) for predictor in predictors},
        timer=timer,
    )
    released = released_labels(events)
    applied = 0
    for due, group in admission_schedule(events, stream.moments, released):
        for index in due:
            _learn(predictors, rows, events, index)
        applied += len(due)
        for index in group:
            _predict_row(predictors, rows[index], index, answers)
    return _Replay(
        answers.probabilities,
        answers.elapsed,
        applied,
        len(released) - applied,
    )


def _learn(
    predictors: cabc.Sequence[Predictor],
    rows: cabc.Sequence[Features | None],
    events: cabc.Sequence[Event],
    index: int,
) -> None:
    """Deliver one released label to every predictor."""
    label = scored_label(events[index])
    if label is not None:
        update_all(predictors, typ.cast("Features", rows[index]), label)


def _predict_row(
    predictors: cabc.Sequence[Predictor],
    row: Features | None,
    index: int,
    answers: _Answers,
) -> None:
    """Ask every predictor about one predictable row and record the answer.

    The clock travels with the answers it fills, so every recorded interval
    comes from the same source as the columns it is written into.
    """
    known = typ.cast("Features", row)
    for predictor in predictors:
        started = answers.timer()
        answer = predictor.predict(known)
        answers.elapsed[predictor.name][index] = answers.timer() - started
        answers.probabilities[predictor.name][index] = answer
