"""Pure gate for deciding whether an outcome statistics read is quiet."""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from buildlimits.admission_state import Counts
    from buildlimits.outcome_vocabulary import GateReason


@dc.dataclass(frozen=True, slots=True)
class QuietInputs:
    """Identifier-free inputs needed to decide whether a reading may start.

    ``has_baseline`` separates the two jobs a reading can do. Establishing a
    baseline only snapshots the counters, so moving work and detached drain do
    not make it unsafe: any request already in the window is accounted for,
    and a launch during the query is refused by the straddle rule. Attributing
    a window needs every launch since the baseline to have ended, so moving
    work and detached drain remain blocking clauses there.
    """

    counts: Counts
    is_reachable: bool
    is_cache_allowed: bool
    is_recovery_idle: bool
    has_barrier: bool
    has_query_in_flight: bool
    has_baseline: bool = True


def quiet_point(inputs: QuietInputs) -> GateReason | None:
    """Return the first false gate clause, or ``None`` for a quiet point.

    The queue depth is deliberately not a clause. A queued request that starts
    during the query is already detected by the straddle rule, which refuses
    the whole reading, so requiring an empty queue added no safety and made a
    reading impossible on a host that keeps work queued continuously.

    A baseline reading does not require a motionless host either: it only needs
    the safety clauses, because it snapshots the counters without attributing
    any window. An attributing reading additionally requires no cached group
    to be moving and no detached work to be draining, so every launch in its
    window has ended cleanly.

    Returns
    -------
    GateReason or None
        The first blocked gate clause, or ``None`` when every clause is clear.

    Examples
    --------
    >>> from buildlimits.admission_state import Counts
    >>> counts = Counts(0, 0, 0, 0, 0, 1, 0)
    >>> inputs = QuietInputs(
    ...     counts=counts,
    ...     is_reachable=True,
    ...     is_cache_allowed=True,
    ...     is_recovery_idle=True,
    ...     has_barrier=False,
    ...     has_query_in_flight=False,
    ... )
    >>> quiet_point(inputs) is None
    True
    >>> queued = dc.replace(inputs, counts=dc.replace(counts, queued=647))
    >>> quiet_point(queued) is None
    True
    >>> first = dc.replace(queued, has_baseline=False)
    >>> quiet_point(first) is None
    True
    >>> moving = dc.replace(
    ...     first, counts=dc.replace(first.counts, cached_moving=3)
    ... )
    >>> quiet_point(moving) is None
    True
    """
    clauses: list[tuple[bool, GateReason]] = []
    if inputs.has_baseline:
        clauses += [
            (inputs.counts.cached_moving == 0, "cached_moving"),
            (inputs.counts.server_drain == 0, "server_drain"),
        ]
    clauses += [
        (inputs.is_reachable, "unreachable"),
        (inputs.is_cache_allowed, "held"),
        (inputs.is_recovery_idle, "recovering"),
        (not inputs.has_barrier, "barrier"),
        (not inputs.has_query_in_flight, "in_flight"),
    ]
    return next((reason for is_clear, reason in clauses if not is_clear), None)
