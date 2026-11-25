"""Pure gate for deciding whether an outcome statistics read is quiet."""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from buildlimits.admission_state import Counts
    from buildlimits.outcome_vocabulary import GateReason


@dc.dataclass(frozen=True, slots=True)
class QuietInputs:
    """Identifier-free inputs needed to decide whether a reading may start.

    Every reading needs the same boundary. A baseline is not the cheaper job it
    looks like: sccache increments its counters when a compilation finishes, so
    a request still in flight when the baseline is taken contributes to the
    *next* window's delta while belonging to no window at all. That window
    cannot balance, and the label it would have carried is lost. A baseline is
    therefore taken only when no cached compilation is in flight, exactly as an
    attributing reading is.
    """

    counts: Counts
    is_reachable: bool
    is_cache_allowed: bool
    is_recovery_idle: bool
    has_barrier: bool
    has_query_in_flight: bool


def quiet_point(inputs: QuietInputs) -> GateReason | None:
    """Return the first false gate clause, or ``None`` for a quiet point.

    The queue depth is deliberately not a clause. A queued request that starts
    during the query is already detected by the straddle rule, which refuses
    the whole reading, so requiring an empty queue added no safety and made a
    reading impossible on a host that keeps work queued continuously.

    Cached movement and detached drain are clauses for a baseline as well as
    for an attributing reading. Their counters are cumulative, so a cached
    compilation that is still in flight when the boundary is read adds to the
    following delta without appearing in the following window. Requiring both
    to be clear means every delta is composed only of launches the window
    holds.

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
    >>> moving = dc.replace(queued, counts=dc.replace(counts, cached_moving=3))
    >>> quiet_point(moving)
    'cached_moving'
    """
    clauses: list[tuple[bool, GateReason]] = [
        (inputs.counts.cached_moving == 0, "cached_moving"),
        (inputs.counts.server_drain == 0, "server_drain"),
        (inputs.is_reachable, "unreachable"),
        (inputs.is_cache_allowed, "held"),
        (inputs.is_recovery_idle, "recovering"),
        (not inputs.has_barrier, "barrier"),
        (not inputs.has_query_in_flight, "in_flight"),
    ]
    return next((reason for is_clear, reason in clauses if not is_clear), None)
