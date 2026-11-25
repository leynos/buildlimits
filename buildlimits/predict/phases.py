"""The tuning/held-out split, placed between label batches.

The boundary falls between batches, never inside one, so every row the
observer accepted at one reading stays on one side of it. Placing it is a
question about label evidence alone, which is why it lives apart from the
walk that consumes it: the harness asks for phases and a split, and nothing
else in this module knows how a prediction is made.
"""

import dataclasses as dc
import typing as typ

from buildlimits.predict.events import label_moment, scored_label
from buildlimits.predict.history import label_batches

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.predict.events import Event
    from buildlimits.predict.metrics import Phase
    from buildlimits.predict.timing import TimingAudit


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


def split(events: cabc.Sequence[Event], phases: cabc.Sequence[Phase | None]) -> Split:
    """Summarize the tuning/held-out boundary.

    Returns
    -------
    Split
        Counts on both sides and the first held-out availability time.
    """
    boundary_index = next(
        (index for index, phase in enumerate(phases) if phase == "held-out"), None
    )
    return Split(
        train_labelled=sum(1 for phase in phases if phase == "train"),
        heldout_labelled=sum(1 for phase in phases if phase == "held-out"),
        boundary=(
            None if boundary_index is None else label_moment(events[boundary_index])
        ),
    )


def phases(
    events: cabc.Sequence[Event],
    timing: TimingAudit,
    *,
    train_fraction: float,
) -> tuple[Phase | None, ...]:
    """Label each event with the phase its verified label time belongs to.

    A batch that reaches the target lands wholly in the tuning period, which
    can leave the tuning share slightly above the configured fraction. With too
    few labels to reach the boundary every batch is tuning; at a fraction of
    zero every batch is held out. An event without a verified availability time
    cannot be placed in either period: its label was never shown to be
    knowable, so it is never trained on or scored.

    Returns
    -------
    tuple[Phase | None, ...]
        One phase per event, or None where no eligible label arrived.
    """
    batches = label_batches(events)
    target = int(_scorable_labels(batches, events, timing) * train_fraction)
    assigned: list[Phase | None] = [None] * len(events)
    kept = 0
    for batch in batches:
        if label_moment(events[batch[0]]) is None:
            continue
        phase: Phase = "train" if kept < target else "held-out"
        for index in batch:
            if _is_scorable(events[index], is_usable=timing.usable[index]):
                assigned[index] = phase
                kept += 1
    return tuple(assigned)


def _is_scorable(event: Event, *, is_usable: bool) -> bool:
    """Return whether one event may occupy a training or held-out slot.

    Only a verified, correlated label can be scored, so only it consumes a
    phase slot or contributes to the boundary the split is placed at.

    Returns
    -------
    bool
        Whether the event is usable and carries a scored label.
    """
    return is_usable and scored_label(event) is not None


def _scorable_labels(
    batches: cabc.Sequence[tuple[int, ...]],
    events: cabc.Sequence[Event],
    timing: TimingAudit,
) -> int:
    """Count the events that can consume a phase slot.

    Returns
    -------
    int
        The number of scorable events across every label batch.
    """
    return sum(
        1
        for batch in batches
        for index in batch
        if _is_scorable(events[index], is_usable=timing.usable[index])
    )
