"""Pure gate for deciding whether an outcome statistics read is quiet."""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from buildlimits.admission_state import Counts
    from buildlimits.outcome_vocabulary import GateReason


@dc.dataclass(frozen=True, slots=True)
class QuietInputs:
    """Identifier-free inputs needed to decide whether a reading may start."""

    counts: Counts
    is_reachable: bool
    is_cache_allowed: bool
    is_recovery_idle: bool
    has_barrier: bool
    has_query_in_flight: bool


def quiet_point(inputs: QuietInputs) -> GateReason | None:
    """Return the first false gate clause, or ``None`` for a quiet point.

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
    """
    clauses: tuple[tuple[bool, GateReason], ...] = (
        (inputs.counts.queued == 0, "queued"),
        (inputs.counts.cached_moving == 0, "cached_moving"),
        (inputs.counts.server_drain == 0, "server_drain"),
        (inputs.is_reachable, "unreachable"),
        (inputs.is_cache_allowed, "held"),
        (inputs.is_recovery_idle, "recovering"),
        (not inputs.has_barrier, "barrier"),
        (not inputs.has_query_in_flight, "in_flight"),
    )
    return next((reason for is_clear, reason in clauses if not is_clear), None)
