"""Map requester lifecycle metadata to bounded terminal measurements.

Requests alone decides when to emit or release ownership. This pure adapter
never changes a Pending entry, owns no descriptors and retains no identifiers.
"""

import typing as typ

from buildlimits.metrics import gauge_fields, refusal_reason

if typ.TYPE_CHECKING:
    from buildlimits.admission_state import Counts
    from buildlimits.pending_request import Pending


def finished_fields(
    entry: Pending, now: float, *, outcome: str, reason: str = "none"
) -> dict[str, object]:
    """Translate one terminal boundary without mutating request state.

    Parameters
    ----------
    entry : Pending
        Existing requester lifecycle metadata; identifiers are excluded.
    now : float
        Monotonic timestamp sampled by the application boundary.
    outcome : str
        Bounded terminal result selected by the request owner.
    reason : str
        Bounded refusal token, or ``none``.

    Returns
    -------
    dict[str, object]
        Self-contained result, cancellation, cache path and duration fields.
    """
    admitted = entry.admitted
    return {
        "outcome": "cancelled"
        if entry.cancellation != "none" and outcome not in {"exec_failed", "refused"}
        else outcome,
        "reason": "exec_failed" if entry.exec_error is not None else reason,
        "cancellation": entry.cancellation,
        "cache_path": entry.cache_path,
        "wait_seconds": (now if admitted is None else admitted) - entry.arrived,
        "runtime_seconds": 0.0 if admitted is None else now - admitted,
    }


def rejected_fields(reason: str) -> dict[str, object]:
    """Map an unrecorded refusal to identifier-free terminal measurements.

    Parameters
    ----------
    reason : str
        Protocol refusal reason, normalized to the fixed metric vocabulary.

    Returns
    -------
    dict[str, object]
        Refusal with no cancellation and zero queue or compile duration.
    """
    return {
        "outcome": "refused",
        "reason": refusal_reason(reason),
        "cancellation": "none",
        "wait_seconds": 0.0,
        "runtime_seconds": 0.0,
    }


def admission_fields(counts: Counts | None) -> dict[str, object]:
    """Map an optional admission snapshot without identifiers.

    Parameters
    ----------
    counts : Counts | None
        Current domain snapshot, or absent for a standalone requester port.

    Returns
    -------
    dict[str, object]
        Six validated gauge names, or no fields when the snapshot is absent.
    """
    return {} if counts is None else dict(gauge_fields(counts))
