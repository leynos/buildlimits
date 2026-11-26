"""Count the label populations the evidence gates read.

The minimum-label gate needs labels that are independent *and* time-verified,
and a report that fails it has to say which of those the history lacked. That
needs more than one number: the recorded labels, the independent ones, their
intersection, and the independent labels whose decision or availability time
survived the audit. Keeping the counts in one value decided from one stream and
one audit is what stops two callers reading different populations.

The two timing counts cover the independent labels only. A label whose
independence is unknown was never eligible to count either way, so it cannot
say which half of the timing evidence is missing.
"""

import dataclasses as dc
import typing as typ

from buildlimits.predict.events import admission_moment, label_moment
from buildlimits.predict.timing import CORRELATION_REASONS, TimingAudit

if typ.TYPE_CHECKING:
    from buildlimits.predict.events import Event

__all__ = ["Population", "independence", "population"]


@dc.dataclass(frozen=True, slots=True)
class Population:
    """The label and time counts a walk reports beside its predictions.

    They are read together by the evidence gates, and kept apart from the
    replay so that each count is decided once from the same stream and audit.
    Every count names a distinct population: the recorded labels, the
    independent ones, their joint intersection, and the two timing halves that
    say which evidence a failing gate is short of.
    """

    labelled: int
    independent_labelled: int
    usable_labelled: int
    admission_usable_labelled: int
    availability_usable_labelled: int
    unavailable_admission: int
    unavailable_availability: int


def independence(stream: tuple[Event, ...], timing: TimingAudit) -> tuple[bool, ...]:
    """Return whether each row is an independent label.

    The same predicate the gate counts is published per position, so a
    consumer that needs independent evidence — the held-out comparison, for
    one — asks the same question the gate asked rather than re-deriving it.

    Returns
    -------
    tuple[bool, ...]
        One flag per row, in stream order.
    """
    return tuple(
        _is_independent(event, timing.reasons[index])
        for index, event in enumerate(stream)
    )


def _decision_usable(event: Event, reason: str | None) -> bool:
    """Return whether the row's decision time survives the audit.

    Each half is asked of the row's own evidence, not of the audit's first
    closed reason. ``timing_exclusion`` reports one reason, so a row missing
    both times is excluded as ``no_admission_time``; reading that reason as
    proof that its availability was fine would count the row twice over.

    Returns
    -------
    bool
        Whether the row records a decision the clock can order.
    """
    return event.admitted_at is not None and reason != "admission_after_availability"


def _availability_usable(event: Event, reason: str | None) -> bool:
    """Return whether the row's availability time survives the audit.

    Returns
    -------
    bool
        Whether the row proves its label time and no closed reason withholds
        the other half of the pair it must be ordered against.
    """
    return label_moment(event) is not None and reason not in _UNAVAILABILITY_REASONS


def _is_independent(event: Event, reason: str | None) -> bool:
    """Return whether a row is an independent label.

    Returns
    -------
    bool
        Whether the row carries a hit or miss label, names a complete attempt
        pair, and no correlation exclusion rejects it.
    """
    return (
        event.label != "unknown"
        and event.attempt is not None
        and event.supervisor_generation is not None
        and reason not in CORRELATION_REASONS
    )


_UNAVAILABILITY_REASONS = frozenset({
    "no_availability_time",
    "completion_after_availability",
})
"""Reasons that say the row's own label time is not usable.

The two admission reasons are deliberately absent: a row whose decision is
missing or late still states when its label became known, and the audit's
first closed reason must not be read as evidence that the availability is
missing too. A row missing both times has no ``label_moment`` at all, so it
counts as unavailable here on its own evidence.
"""


@dc.dataclass(slots=True)
class _Tally:
    """Accumulate the populations one row at a time."""

    labelled: int = 0
    independent: int = 0
    usable: int = 0
    admission_usable: int = 0
    availability_usable: int = 0

    def add(self, event: Event, reason: str | None) -> None:
        """Fold one row's identity and timing evidence into the counts."""
        if event.label != "unknown":
            self.labelled += 1
        if not _is_independent(event, reason):
            return
        self.independent += 1
        if reason is None:
            self.usable += 1
        if _decision_usable(event, reason):
            self.admission_usable += 1
        if _availability_usable(event, reason):
            self.availability_usable += 1


def population(stream: tuple[Event, ...], timing: TimingAudit) -> Population:
    """Count the labelled and timed rows the evidence gates read.

    The gate population is the intersection of the two properties, not
    either one alone: a label the audit withholds cannot support a verdict
    just because it names a distinct attempt, and a legacy row cannot
    support one just because its time was verified.

    Returns
    -------
    Population
        The labelled population, the independent part of it, the jointly
        usable part, and the rows missing an admission or availability time.
    """
    tally = _Tally()
    for index, event in enumerate(stream):
        tally.add(event, timing.reasons[index])
    return Population(
        labelled=tally.labelled,
        independent_labelled=tally.independent,
        usable_labelled=tally.usable,
        admission_usable_labelled=tally.admission_usable,
        availability_usable_labelled=tally.availability_usable,
        unavailable_admission=sum(
            1 for event in stream if admission_moment(event) is None
        ),
        unavailable_availability=sum(
            1
            for event in stream
            if admission_moment(event) is not None and label_moment(event) is None
        ),
    )
