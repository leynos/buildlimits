"""The shared context and result values for one archive run.

These values are passed unchanged through every per-segment operation, so they
live apart from the operations that consume them and can be imported without
pulling in the archive filesystem machinery.
"""

import dataclasses as dc
import typing as typ

from buildlimits.retry_notice import ignore_retry
from buildlimits.utc_text import utc_now

if typ.TYPE_CHECKING:
    import collections.abc as cabc
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

__all__ = [
    "ActionName",
    "ActionResult",
    "ArchiveContext",
    "ignore_retry",
]


def _required_token() -> str:
    """Refuse a context built without a quarantine-token source.

    Raises
    ------
    TypeError
        Always, so no default source is bound inside this value module.
    """
    msg = "ArchiveContext requires a quarantine_token source"
    raise TypeError(msg)


@dc.dataclass(frozen=True, slots=True)
class ArchiveContext:
    """The layout, policy, moment and services one archive run works with.

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
    clock : Callable[[], datetime]
        The UTC clock read for the report's finish time, so a test can pin
        the whole report instead of only its start.
    quarantine_token : Callable[[], str]
        The source of the random tag in a quarantine file's name.
    on_retry : Callable[[str, int, str], None]
        Receives the operation, attempt number and closed category of every
        reading the run retries, so a caller can report a store that keeps
        moving; the default discards them.
    """

    layout: SegmentLayout
    policy: RetentionPolicy
    preset: int
    now: dt.datetime
    clock: cabc.Callable[[], dt.datetime] = utc_now
    quarantine_token: cabc.Callable[[], str] = _required_token
    on_retry: cabc.Callable[[str, int, str], None] = ignore_retry


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
        A closed fault reason when the operation quarantined something.
    """

    action: ActionName
    sequence: int
    logical_bytes: int = 0
    physical_bytes: int = 0
    reason: str | None = None
