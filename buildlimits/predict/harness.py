"""The embargoed prequential walk over one event stream.

Every event is predicted from what earlier events already revealed, and only
then may its label update anyone. Outcomes that arrive together are predicted
together: a batch of rows is answered in full before any of its labels is
applied, so two outcomes that became known at the same moment cannot inform
each other. The batch immediately before the predicted one is withheld as well:
its labels may have been written after this batch's builds started, so each
prediction uses a history ending two batches back. The order is the store's
append order, which is the order labels were written; nothing is sorted or
shuffled, so a backward clock step cannot move a record into another window.
"""

import dataclasses as dc
import time
import typing as typ

from buildlimits.predict.events import (
    Features,
    features,
    label_batches,
    scored_label,
)
from buildlimits.predict.predictors import single_predictors, update_all

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.predict.config import Config
    from buildlimits.predict.events import Event
    from buildlimits.predict.metrics import Phase
    from buildlimits.predict.predictors import Predictor


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
        The stream, in the store's append order.
    rows : tuple[Features, ...]
        Causal features, aligned with ``events``.
    phases : tuple[Phase | None, ...]
        The phase of each labelled event, or None for an unscored one.
    series : dict[str, Series]
        Answers per single predictor, in creation order.
    split : Split
        The tuning/held-out boundary.
    batches : tuple[tuple[int, ...], ...]
        Event indices grouped by equal label time, in stream order.
    embargoed_rows : int
        Eligible labels each withheld from one batch's predictions by the
        one-batch embargo.
    unapplied_rows : int
        Eligible labels in the final batch, which no prediction follows.
    """

    events: tuple[Event, ...]
    rows: tuple[Features, ...]
    phases: tuple[Phase | None, ...]
    series: dict[str, Series]
    split: Split
    batches: tuple[tuple[int, ...], ...]
    embargoed_rows: int
    unapplied_rows: int

    def components(self) -> tuple[str, ...]:
        """Return the single predictor names in their fixed order.

        Returns
        -------
        tuple[str, ...]
            Component names.
        """
        return tuple(self.series)


def walk_forward(events: cabc.Iterable[Event], config: Config) -> Walk:
    """Predict every event in order, updating from embargoed labels.

    Each batch is predicted in full before any label is applied, and the batch
    before it is applied only afterwards, so a prediction's history ends two
    batches back. The final batch's labels inform nothing and are left
    unapplied rather than reported as if some prediction had learned them.

    Returns
    -------
    Walk
        The stream, its features, phases and each single predictor's answers.
    """
    stream = tuple(events)
    rows = features(stream, load_window_seconds=config.load_window_seconds)
    batches = label_batches(stream)
    phases = _phases(stream, batches, train_fraction=config.train_fraction)
    replay = _replay(single_predictors(config), rows, stream, batches)
    series = {
        name: Series(
            name, tuple(replay.probabilities[name]), tuple(replay.elapsed[name])
        )
        for name in replay.probabilities
    }
    return Walk(
        events=stream,
        rows=rows,
        phases=phases,
        series=series,
        split=_split(stream, phases),
        batches=batches,
        embargoed_rows=replay.embargoed_rows,
        unapplied_rows=replay.unapplied_rows,
    )


@dc.dataclass(frozen=True, slots=True)
class _Replay:
    """One batch-scheduled pass over the stream.

    Attributes
    ----------
    probabilities : dict[str, list[float | None]]
        Answers per predictor, in stream order.
    elapsed : dict[str, list[float]]
        Seconds spent predicting each event, per predictor.
    embargoed_rows : int
        Eligible labels withheld one batch by the embargo.
    unapplied_rows : int
        Eligible labels in the final batch, which no prediction follows.
    """

    probabilities: dict[str, list[float | None]]
    elapsed: dict[str, list[float]]
    embargoed_rows: int
    unapplied_rows: int


def _replay(
    predictors: cabc.Sequence[Predictor],
    rows: cabc.Sequence[Features],
    events: cabc.Sequence[Event],
    batches: cabc.Sequence[tuple[int, ...]],
) -> _Replay:
    """Answer every batch, applying each pending batch only afterwards.

    Returns
    -------
    _Replay
        The answers, timings and the embargo accounting.
    """
    probabilities: dict[str, list[float | None]] = {
        predictor.name: [] for predictor in predictors
    }
    elapsed: dict[str, list[float]] = {predictor.name: [] for predictor in predictors}
    pending: tuple[int, ...] | None = None
    embargoed = 0
    for batch in batches:
        for index in batch:
            _predict_row(predictors, rows[index], probabilities, elapsed)
        if pending is not None:
            _apply_batch(predictors, rows, events, pending)
            embargoed += _eligible_count(events, pending)
        pending = batch
    unapplied = 0 if pending is None else _eligible_count(events, pending)
    return _Replay(probabilities, elapsed, embargoed, unapplied)


def _predict_row(
    predictors: cabc.Sequence[Predictor],
    row: Features,
    probabilities: dict[str, list[float | None]],
    elapsed: dict[str, list[float]],
) -> None:
    """Ask every predictor about one row and record its answer and time."""
    for predictor in predictors:
        started = time.perf_counter()
        answer = predictor.predict(row)
        elapsed[predictor.name].append(time.perf_counter() - started)
        probabilities[predictor.name].append(answer)


def _apply_batch(
    predictors: cabc.Sequence[Predictor],
    rows: cabc.Sequence[Features],
    events: cabc.Sequence[Event],
    batch: cabc.Sequence[int],
) -> None:
    """Deliver one batch's eligible labels to every predictor."""
    for index in batch:
        label = scored_label(events[index])
        if label is not None:
            update_all(predictors, rows[index], label)


def _eligible_count(events: cabc.Sequence[Event], batch: cabc.Sequence[int]) -> int:
    """Return how many labels in one batch can train a predictor.

    Returns
    -------
    int
        The number of hit/miss labels in the batch.
    """
    return sum(1 for index in batch if scored_label(events[index]) is not None)


def _phases(
    events: cabc.Sequence[Event],
    batches: cabc.Sequence[cabc.Sequence[int]],
    *,
    train_fraction: float,
) -> tuple[Phase | None, ...]:
    """Label each event with the phase its batch belongs to.

    The boundary falls between batches, never inside one, so every row the
    store wrote together stays on one side of it. A batch that reaches the
    target lands wholly in the tuning period, which can leave the tuning share
    slightly above the configured fraction. With too few labels to reach the
    boundary every batch is tuning; at a fraction of zero every batch is held
    out.

    Returns
    -------
    tuple[Phase | None, ...]
        One phase per event, or None where no eligible label arrived.
    """
    total = sum(1 for event in events if scored_label(event) is not None)
    target = int(total * train_fraction)
    phases: list[Phase | None] = []
    kept = 0
    for batch in batches:
        phase: Phase = "train" if kept < target else "held-out"
        for index in batch:
            if scored_label(events[index]) is None:
                phases.append(None)
            else:
                phases.append(phase)
                kept += 1
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
