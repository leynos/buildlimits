"""The shared context and result values for one archive run.

These values are passed unchanged through every per-segment operation, so they
live apart from the operations that consume them and can be imported without
pulling in the archive filesystem machinery.
"""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import datetime as dt

    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_segments import SegmentLayout

type ActionName = typ.Literal[
    "archived",
    "reconciled",
    "re-archived",
    "trimmed",
    "deleted",
    "repaired",
    "quarantined",
]

__all__ = ["ActionName", "ActionResult", "ArchiveContext"]


@dc.dataclass(frozen=True, slots=True)
class ArchiveContext:
    """The layout, policy and moment one archive run works with.

    Attributes
    ----------
    layout : SegmentLayout
        The store's segment layout.
    policy : RetentionPolicy
        The effective retention and headroom policy.
    preset : int
        The XZ preset used for new and rewritten archives.
    now : datetime
        The run's caller-supplied UTC wall time.
    """

    layout: SegmentLayout
    policy: RetentionPolicy
    preset: int
    now: dt.datetime


@dc.dataclass(frozen=True, slots=True)
class ActionResult:
    """What one segment operation did, for the run report.

    Attributes
    ----------
    action : ActionName
        The closed operation name.
    sequence : int
        The segment sequence.
    logical_bytes : int
        Logical bytes the operation produced; zero for a deletion.
    physical_bytes : int
        Bytes the archive occupies after the operation.
    reason : str | None
        A closed reason for the operation. A quarantine names the fault; a
        deletion or trim names every bound that caused it, from ``age``,
        ``bytes`` and ``records``, joined by ``_and_`` when a single rewrite
        serves more than one.
    """

    action: ActionName
    sequence: int
    logical_bytes: int = 0
    physical_bytes: int = 0
    reason: str | None = None
