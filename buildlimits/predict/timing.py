"""Bounded timing and attempt-correlation accounting for one history.

The benchmark may score a record only when two independent gates hold: its
decision time is trustworthy and the moment its label became available is
trustworthy. This module classifies every event against those gates and a
third, correlation gate: two rows naming the same attempt must agree, because a
retried append is idempotent only when it carries identical evidence.

Every exclusion has a bounded reason drawn from a closed vocabulary, so a
report can count exactly why a record was not scored. Nothing is repaired: a
missing or contradictory time is excluded, never inferred from a completion
timestamp, a runtime, or a neighbouring row.
"""

import dataclasses as dc
import typing as typ

from buildlimits.predict.events import Event, timing_exclusion

if typ.TYPE_CHECKING:
    import collections.abc as cabc

type ExclusionReason = typ.Literal[
    "unknown_outcome",
    "no_admission_time",
    "no_availability_time",
    "admission_after_availability",
    "duplicate_attempt",
    "conflicting_attempt",
]

EXCLUSION_REASONS: tuple[ExclusionReason, ...] = (
    "unknown_outcome",
    "no_admission_time",
    "no_availability_time",
    "admission_after_availability",
    "duplicate_attempt",
    "conflicting_attempt",
)


_MINIMUM_GROUP = 2
"""A repeated attempt needs at least two rows to be a duplicate or a conflict."""


@dc.dataclass(frozen=True, slots=True)
class TimingAudit:
    """Which events may be predicted, scored, and why the rest may not.

    An event with an unknown outcome is still predictable: the harness may ask
    a predictor about it, and the answer is simply never scored because there is
    no label to score against. Every other exclusion also removes the event
    from the predictable population, because its decision or label timing is
    not trustworthy enough to place it causally.

    Attributes
    ----------
    usable : tuple[bool, ...]
        One flag per event; true when the event may be predicted. An unknown
        outcome is predictable but not scorable.
    reasons : tuple[ExclusionReason | None, ...]
        One closed reason per event, or None where the event is scorable.
    duplicates : int
        Additional rows that repeated an attempt with identical evidence.
    conflicts : int
        Rows whose attempt identity also appeared with different evidence.
    """

    usable: tuple[bool, ...]
    reasons: tuple[ExclusionReason | None, ...]
    duplicates: int
    conflicts: int

    def exclusions(self) -> dict[str, int]:
        """Count each scoring exclusion reason.

        Returns
        -------
        dict[str, int]
            A count per reason, with absent reasons omitted. Every event is
            either scored or counted exactly once.
        """
        counts: dict[str, int] = {}
        for reason in self.reasons:
            if reason is not None:
                counts[reason] = counts.get(reason, 0) + 1
        return dict(sorted(counts.items()))

    def scored(self) -> int:
        """Return how many events may be scored.

        Returns
        -------
        int
            The number of events with a trustworthy decision, a trustworthy
            availability time and a hit or miss label.
        """
        return sum(reason is None for reason in self.reasons)


def audit(events: cabc.Sequence[Event]) -> TimingAudit:
    """Classify every event against the timing, label and correlation gates.

    Parameters
    ----------
    events : Sequence[Event]
        The events in store append order.

    Returns
    -------
    TimingAudit
        Per-event verdicts plus the duplicate and conflict counts.

    Examples
    --------
    A row without a verified availability time is excluded, not backdated:

    >>> from buildlimits.predict.events import EPOCH, Event
    >>> verdict = audit((Event("a" * 64, "git", EPOCH, EPOCH, "hit", 0.1),))
    >>> verdict.usable, verdict.exclusions()
    ((False,), {'no_availability_time': 1})
    """
    stream = tuple(events)
    attempts = _attempts(stream)
    conflicts, duplicates, attempt_reasons = _attempt_verdicts(stream, attempts)
    reasons: list[ExclusionReason | None] = []
    for index, event in enumerate(stream):
        reasons.append(attempt_reasons.get(index) or _event_reason(event))
    # An unknown outcome is predictable but never scorable: the harness may ask
    # for an answer, and there is no label to compare it with.
    return TimingAudit(
        usable=tuple(
            reason is None or reason == "unknown_outcome" for reason in reasons
        ),
        reasons=tuple(reasons),
        duplicates=duplicates,
        conflicts=conflicts,
    )


def _attempts(stream: cabc.Sequence[Event]) -> dict[tuple[str, str], list[int]]:
    """Group event indices by their verified attempt identity.

    Returns
    -------
    dict[tuple[str, str], list[int]]
        Indices per ``(generation, attempt)`` pair, in stream order. An event
        missing either half is absent: it has no joinable identity.
    """
    grouped: dict[tuple[str, str], list[int]] = {}
    for index, event in enumerate(stream):
        if event.supervisor_generation is None or event.attempt is None:
            continue
        key = (event.supervisor_generation, event.attempt)
        grouped.setdefault(key, []).append(index)
    return grouped


def _attempt_verdicts(
    stream: cabc.Sequence[Event], attempts: cabc.Mapping[tuple[str, str], list[int]]
) -> tuple[int, int, dict[int, ExclusionReason]]:
    """Classify repeated and contradictory attempt identities.

    One append may be retried after a crash, so two rows with the same attempt
    identity are allowed only when their recorded evidence is identical; the
    later copy is an idempotent duplicate. Rows that disagree about the same
    attempt are unusable together, because nothing in the document says which
    one the observer accepted.

    Returns
    -------
    tuple[int, int, dict[int, ExclusionReason]]
        The conflicting row count, the duplicate row count, and the per-index
        attempt-level exclusion reasons.
    """
    conflicts = duplicates = 0
    reasons: dict[int, ExclusionReason] = {}
    for indices in attempts.values():
        if len(indices) < _MINIMUM_GROUP:
            continue
        first = _fingerprint(stream[indices[0]])
        if all(_fingerprint(stream[index]) == first for index in indices[1:]):
            for index in indices[1:]:
                reasons[index] = "duplicate_attempt"
            duplicates += len(indices) - 1
            continue
        for index in indices:
            reasons[index] = "conflicting_attempt"
        conflicts += len(indices)
    return conflicts, duplicates, reasons


def _fingerprint(event: Event) -> tuple[object, ...]:
    """Return the recorded evidence that must agree for one attempt.

    Returns
    -------
    tuple[object, ...]
        The identity, label, timing evidence and runtime of the row. A retried
        append reproduces all of them, so any difference is a real conflict.
    """
    return (
        event.identity,
        event.origin,
        event.label,
        event.admitted_at,
        event.completed_at,
        event.label_observed_verified,
        event.observed_at,
        event.runtime_seconds,
    )


def _event_reason(event: Event) -> ExclusionReason | None:
    """Return this event's own timing or label exclusion.

    Returns
    -------
    ExclusionReason | None
        The first failing gate, or None when the event's own evidence is
        usable. The literals are shared with :func:`timing_exclusion`.
    """
    reason = timing_exclusion(event)
    return None if reason is None else typ.cast("ExclusionReason", reason)
