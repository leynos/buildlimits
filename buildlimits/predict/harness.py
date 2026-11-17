"""The prequential walk over one chronological event stream.

Every event is predicted from what earlier events already revealed, and only
then may its label update anyone. Outcomes that arrive together are predicted
together: a batch of rows is answered in full before any of its labels is
applied, so two outcomes that became known at the same moment cannot inform
each other. The order is the data's own order; nothing is shuffled, so a
predictor cannot meet a label before its event.
"""

import dataclasses as dc
import time
import typing as typ

from buildlimits.predict.events import Features, features, ordered, scored_label
from buildlimits.predict.predictors import single_predictors, update_all

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.predict.config import Config
    from buildlimits.predict.events import Event
    from buildlimits.predict.metrics import Phase


@dc.dataclass(frozen=True, slots=True)
class Split:
    """Where the tuning period ends and the held-out period begins.

    Attributes
    ----------
    train_labelled : int
        Labelled events that may inform tuning and weight fitting.
    heldout_labelled : int
        Labelled events reserved for the final comparison.
    boundary : datetime | None
        Label time of the first held-out event, or None without one.
    """

    train_labelled: int
    heldout_labelled: int
    boundary: dt.datetime | None

    def to_dict(self) -> dict[str, object]:
        """Return the report shape for the split.

        Returns
        -------
        dict[str, object]
            Counts and the boundary timestamp.
        """
        return {
            "train_labelled": self.train_labelled,
            "heldout_labelled": self.heldout_labelled,
            "boundary": (None if self.boundary is None else self.boundary.isoformat()),
        }


@dc.dataclass(frozen=True, slots=True)
class Series:
    """One predictor's answers and the wall time each one took.

    Attributes
    ----------
    name : str
        The predictor's stable name.
    probabilities : tuple[float | None, ...]
        One answer per event, in stream order.
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
        The stream, oldest label first.
    rows : tuple[Features, ...]
        Causal features, aligned with ``events``.
    phases : tuple[Phase | None, ...]
        The phase of each labelled event, or None for an unscored one.
    series : dict[str, Series]
        Answers per single predictor, in creation order.
    split : Split
        The tuning/held-out boundary.
    """

    events: tuple[Event, ...]
    rows: tuple[Features, ...]
    phases: tuple[Phase | None, ...]
    series: dict[str, Series]
    split: Split

    def components(self) -> tuple[str, ...]:
        """Return the single predictor names in their fixed order.

        Returns
        -------
        tuple[str, ...]
            Component names.
        """
        return tuple(self.series)


def walk_forward(events: cabc.Iterable[Event], config: Config) -> Walk:
    """Predict every event in order, updating only from delivered labels.

    Returns
    -------
    Walk
        The stream, its features, phases and each single predictor's answers.
    """
    stream = ordered(events)
    rows = features(stream, load_window_seconds=config.load_window_seconds)
    phases = _phases(stream, train_fraction=config.train_fraction)
    predictors = single_predictors(config)
    probabilities: dict[str, list[float | None]] = {
        predictor.name: [] for predictor in predictors
    }
    elapsed: dict[str, list[float]] = {predictor.name: [] for predictor in predictors}
    for batch in _label_batches(rows, stream):
        for row, _ in batch:
            for predictor in predictors:
                started = time.perf_counter()
                answer = predictor.predict(row)
                elapsed[predictor.name].append(time.perf_counter() - started)
                probabilities[predictor.name].append(answer)
        for row, event in batch:
            label = scored_label(event)
            if label is not None:
                update_all(predictors, row, label)
    series = {
        name: Series(name, tuple(probabilities[name]), tuple(elapsed[name]))
        for name in probabilities
    }
    return Walk(
        events=stream,
        rows=rows,
        phases=phases,
        series=series,
        split=_split(stream, phases),
    )


def _label_batches(
    rows: cabc.Sequence[Features], events: cabc.Sequence[Event]
) -> cabc.Iterator[list[tuple[Features, Event]]]:
    """Group consecutive rows whose labels arrived at the same moment.

    A batch is predicted in full before any of its labels is applied: two
    outcomes that became known together could not have informed each other.
    The events are already ordered by label time, so equal times are adjacent.

    Yields
    ------
    list[tuple[Features, Event]]
        One batch per distinct label time, in stream order.
    """
    batch: list[tuple[Features, Event]] = []
    for row, event in zip(rows, events, strict=True):
        if batch and event.observed_at != batch[0][1].observed_at:
            yield batch
            batch = []
        batch.append((row, event))
    if batch:
        yield batch


def _phases(
    events: cabc.Sequence[Event], *, train_fraction: float
) -> tuple[Phase | None, ...]:
    """Label each event with the phase its label belongs to.

    Returns
    -------
    tuple[Phase | None, ...]
        One phase per event, or None where no label arrived.
    """
    labelled = sum(1 for event in events if scored_label(event) is not None)
    boundary = int(labelled * train_fraction)
    phases: list[Phase | None] = []
    seen = 0
    for event in events:
        if scored_label(event) is None:
            phases.append(None)
            continue
        phases.append("train" if seen < boundary else "held-out")
        seen += 1
    return tuple(phases)


def _split(events: cabc.Sequence[Event], phases: cabc.Sequence[Phase | None]) -> Split:
    """Summarize the tuning/held-out boundary.

    Returns
    -------
    Split
        Counts on both sides and the first held-out label time.
    """
    boundary_index = next(
        (index for index, phase in enumerate(phases) if phase == "held-out"), None
    )
    train = sum(1 for phase in phases if phase == "train")
    return Split(
        train_labelled=train,
        heldout_labelled=sum(1 for phase in phases if phase == "held-out"),
        boundary=None if boundary_index is None else events[boundary_index].observed_at,
    )
