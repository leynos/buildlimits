"""How a failed drain check is classified, worded and advised.

The supervisor asks the managed cache whether it is stopped and empty. When
that check fails, three things follow from what the failure proved: a fault
kind, the journal prefix that words it, and the guidance an operator is given.
They live together so a new kind cannot silently reuse another's advice, and
away from :mod:`buildlimits.server_watch` so that watch stays the probing of
the server rather than a catalogue of its wording.
"""

import typing as typ

from buildlimits.cache_backend import (
    CacheError,
    LiveCacheWorkError,
    StoppedPopulatedCacheError,
)

type FaultKind = typ.Literal["live_work", "stopped_populated", "manager"]

# Each fault kind carries one journal prefix and one piece of operator
# guidance, so a new kind cannot silently reuse another's advice.
FAULT_PREFIXES: dict[FaultKind, str] = {
    "live_work": "holding tokens for ",
    "stopped_populated": "holding tokens while the stopped cache is not empty: ",
    "manager": "cannot read the sccache server's load; holding tokens: ",
}

# Operator guidance is chosen by what the failed check actually proved:
# live work needs a coordinated recovery, a stopped populated service needs
# its cgroup inspected, and a transport fault needs the manager restored.
MANDATORY_ACTIONS: dict[FaultKind, str] = {
    "live_work": (
        "Run build-limits-recover to stop the managed sccache service under the "
        "admission barrier, verify it is stopped and empty, and release the "
        "retained tokens; tokens remain held until that verification succeeds."
    ),
    "stopped_populated": (
        "The managed sccache service is stopped but its control group still "
        "holds work. Inspect the surviving processes and wait for them to "
        "exit; tokens remain held until the group is empty."
    ),
    "manager": (
        "Restore the systemd user-manager transport and re-read the managed "
        "sccache service state; do not stop or restart sccache for this fault, "
        "and do not release tokens until the service is verified stopped and "
        "empty."
    ),
}

# Chosen instead of the mandatory live-work text while the supervisor's own
# automatic recovery is due or under way. Agents read the mandatory text as an
# order to escalate, which turned a wait of up to five minutes into a report.
PENDING_LIVE_WORK_ACTION = (
    "Automatic recovery of the managed sccache service is pending and needs no "
    "operator action yet. As a fallback only, if it does not start or "
    "complete, run build-limits-recover to stop the service under the "
    "admission barrier, verify it is stopped and empty, and release the "
    "retained tokens; tokens remain held until that verification succeeds."
)


def journal_prefix(*, is_fallback: bool) -> str:
    """Word the journal line that announces the guidance.

    Parameters
    ----------
    is_fallback : bool
        Whether the operator command is only a fallback for a recovery the
        supervisor has pending.

    Returns
    -------
    str
        The prefix; agents treat ``drain requires operator action`` as an
        order to escalate, so it is kept for the cases that really need one.

    Examples
    --------
    >>> journal_prefix(is_fallback=False)
    'drain requires operator action'
    """
    if is_fallback:
        return (
            "drain recovery is pending automatically; operator action is only "
            "a fallback"
        )
    return "drain requires operator action"


def classify(error: CacheError) -> FaultKind:
    """Classify a drain-check failure for guidance and automatic recovery.

    Returns
    -------
    FaultKind
        ``live_work`` for a live service that may hold detached requests,
        ``stopped_populated`` for a stopped service that still contains work,
        or ``manager`` for any fault reading the service state.
    """
    match error:
        case LiveCacheWorkError():
            return "live_work"
        case StoppedPopulatedCacheError():
            return "stopped_populated"
        case _:
            return "manager"
