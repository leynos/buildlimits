"""The outcome shell's status projections.

Retention is the archiver's own document, assembled from the supervisor's
settings and the writer's live counters. The shell only forwards it, and a
failed read must not fail the status call that asked, so the forwarding and its
bounded failure marker live here rather than among the recorder transitions.
"""

import typing as typ

from buildlimits import supervisor_outcome_setup

if typ.TYPE_CHECKING:
    from buildlimits.outcome_ports import OutcomeStorePort
    from buildlimits.supervisor import Supervisor


def retention(supervisor: Supervisor, writer: OutcomeStorePort) -> dict[str, object]:
    """Return the retention document, or a marker naming a failed read.

    Returns
    -------
    dict[str, object]
        ``limits`` and ``active``, or the bounded failure marker.
    """
    try:
        return supervisor_outcome_setup.retention_document(supervisor, writer)
    except Exception:  # ruff: ignore[blind-except] - a status read must not fail status.
        return {"limits": None, "active": None, "reason": "snapshot_unavailable"}
