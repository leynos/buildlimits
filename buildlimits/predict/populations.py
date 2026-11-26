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

__all__ = ["Population", "population"]


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


_ADMISSION_REASONS = frozenset({"no_admission_time", "admission_after_availability"})
"""Reasons that withhold a row's decision time rather than its label time."""

_AVAILABILITY_REASONS = frozenset({
    "no_availability_time",
    "completion_after_availability",
})
"""Reasons that withhold a row's availability time."""


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


def population(stream: tuple[Event, ...], timing: TimingAudit) -> Population:
    """Count the labelled and timed rows the evidence gates read.

    The gate population is the intersection of the two properties, not
    either one alone: a label the audit withholds cannot support a verdict
    just because it names a distinct attempt, and a legacy row cannot
    support one just because its time was verified.

    Returns
    -------
    _Population
        The labelled population, the independent part of it, the jointly
        usable part, and the rows missing an admission or availability time.
    """
    labelled = 0
    independent = 0
    usable = 0
    admission_usable = 0
    availability_usable = 0
    for index, event in enumerate(stream):
        reason = timing.reasons[index]
        if event.label != "unknown":
            labelled += 1
        if not _is_independent(event, reason):
            continue
        independent += 1
        if reason is None:
            usable += 1
        if reason not in _ADMISSION_REASONS:
            admission_usable += 1
        if reason not in _AVAILABILITY_REASONS:
            availability_usable += 1
    return Population(
        labelled=labelled,
        independent_labelled=independent,
        usable_labelled=usable,
        admission_usable_labelled=admission_usable,
        availability_usable_labelled=availability_usable,
        unavailable_admission=sum(
            1 for event in stream if admission_moment(event) is None
        ),
        unavailable_availability=sum(
            1
            for event in stream
            if admission_moment(event) is not None and label_moment(event) is None
        ),
    )
