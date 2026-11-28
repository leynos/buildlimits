"""The correlation evidence one recorded row may carry.

A row either names one invocation or none of it. The group is the bounded
attempt identifier together with the supervisor generation whose clock stamped
the row, and the launch's own completion moment travels beside them. Keeping
the value and its extractor apart from the emission logic means the recorder
and its tests agree on what "one attempt" means without either reading the
other's window handling.
"""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import datetime as dt

    from buildlimits.outcome_recorder_types import _Launch


@dc.dataclass(frozen=True, slots=True)
class TimingEvidence:
    """The complete version 5 correlation group one row may carry.

    A typed value rather than a mapping keeps each field's own type, so a
    completion time cannot be read as an attempt identifier. The moment itself
    is the reading's own, supplied by the emission; this value adds only the
    provenance that says which invocation the row belongs to.
    """

    completed_at: dt.datetime | None
    attempt: str
    supervisor_generation: str


def timing_evidence(launch: _Launch) -> TimingEvidence | None:
    """Return the version 5 correlation group for one launch, or no group.

    The moment a row claims is the reading's own, which the recorder stamps
    from the event that produced it; the label-time provenance marker beside it
    records that the observer accepted the reading. What this group adds is
    identity: the attempt that produced the row and the supervisor generation
    whose clock dated it. A payload without a generation predates that
    evidence, so no group is written and the row states an explicit null,
    which the replay reads as uncorrelated rather than as proof.

    Returns
    -------
    TimingEvidence | None
        The launch's completion moment and correlation tokens, or None.

    Examples
    --------
    A payload that names no supervisor generation yields no group.
    """
    generation = launch.payload.supervisor_generation
    if generation is None:
        return None
    return TimingEvidence(
        completed_at=launch.completed_at,
        attempt=launch.group_id,
        supervisor_generation=generation,
    )


__all__ = ["TimingEvidence", "timing_evidence"]
