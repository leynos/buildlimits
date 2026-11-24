"""The closed eviction vocabulary and the counters a report prints.

A trim or deletion names the bounds that caused it, and the plain report
counts those names. Keeping the vocabulary apart from the report value keeps
the closed words in one place for the planner, the executor and the renderer.
"""

import typing as typ

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive_context import ActionResult

# The closed vocabulary a report counts evictions under. A trim that serves
# more than one bound names each of them, so every combination is listed.
EVICTION_CAUSES = (
    "age",
    "bytes",
    "records",
    "age_and_bytes",
    "age_and_records",
    "bytes_and_records",
    "age_and_bytes_and_records",
)


__all__ = ["EVICTION_CAUSES", "eviction_counts"]


def eviction_counts(actions: tuple[ActionResult, ...]) -> dict[str, int]:
    """Count the trims and deletions by the bound that caused them.

    Parameters
    ----------
    actions : tuple[ActionResult, ...]
        The operations a run performed.

    Returns
    -------
    dict[str, int]
        Closed counts keyed by eviction cause, always present so a reader can
        compare two runs without checking whether a key exists.

    Examples
    --------
    >>> counts = eviction_counts(())
    >>> {cause: count for cause, count in counts.items() if count}
    {}
    >>> list(counts) == list(EVICTION_CAUSES)
    True
    """
    counts = dict.fromkeys(EVICTION_CAUSES, 0)
    for action in actions:
        reason = _eviction_cause(action)
        if reason is not None and reason in counts:
            counts[reason] += 1
    return counts


def _eviction_cause(action: ActionResult) -> str | None:
    """Return the eviction cause an action names, if it is an eviction.

    Returns
    -------
    str | None
        The closed cause for a trim or deletion, or None for every other
        action and for a reason that is not a cause.
    """
    reason = action.reason
    if action.action in {"trimmed", "deleted"} and isinstance(reason, str):
        return reason
    return None
