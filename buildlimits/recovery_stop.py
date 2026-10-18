"""The one guard that stands between a recovery and a stop of the cache.

A cache whose state cannot be read must not be stopped: the stop would be a
guess, and the manager that is meant to perform it is the part that is broken.
That is a different question from whether a transaction may open or whether
the stop must wait, which :mod:`buildlimits.recovery_starvation` answers.
"""

import typing as typ

from buildlimits.systemd import SystemdError

if typ.TYPE_CHECKING:
    from buildlimits.sccache import ManagedSccache


def request_stop(cache: ManagedSccache, fault_kind: str | None) -> None:
    """Ask the manager to stop the managed cache under the barrier.

    Parameters
    ----------
    cache : ManagedSccache
        The managed server, whose control verbs queue jobs.
    fault_kind : str | None
        The drain probe's last fault classification, if any.

    Raises
    ------
    SystemdError
        If the manager refuses the job, cannot be reached, or last failed a
        drain query for transport reasons.
    """
    if fault_kind == "manager":
        msg = (
            f"cannot stop {cache.unit}: the user manager transport is "
            "faulted; restore it and the cache's state is readable again"
        )
        raise SystemdError(msg)
    return cache.stop()
