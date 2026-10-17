"""Assemble supervisor observation ports after resource owners are constructed.

This application boundary shares cached snapshots and fixed drain counters. It
never opens resources or changes admission, launch or shutdown decisions.
"""

import typing as typ

from buildlimits.drain_metrics import DrainMetrics
from buildlimits.listener_pressure import ListenerPressure
from buildlimits.metrics_pressure import supervisor_metrics

if typ.TYPE_CHECKING:
    from buildlimits.supervisor import Supervisor


def configure(supervisor: Supervisor) -> ListenerPressure:
    """Wire one pressure port and one drain observer to existing collaborators.

    Parameters
    ----------
    supervisor : Supervisor
        Constructed admission, requester, group, server and loop owners.

    Returns
    -------
    ListenerPressure
        Listener policy sharing the supervisor's observation port.
    """
    supervisor.metrics = supervisor_metrics(supervisor, supervisor.metrics)
    supervisor.requests.metrics = supervisor.metrics
    supervisor.server.metrics = supervisor.metrics
    # Recovery is the one owner whose port is set at construction and again
    # here: its per-transition emissions are the only record that a transaction
    # opened, stalled or gave up, so it must share whatever port the supervisor
    # ended up observing through rather than a default that discards.
    supervisor.recovery.metrics = supervisor.metrics
    pressure = ListenerPressure(
        supervisor.selector, supervisor.clock, metrics=supervisor.metrics
    )
    drain = DrainMetrics(supervisor.metrics)
    supervisor.groups.drain_metrics = drain
    supervisor.server.drain_metrics = drain
    supervisor.loop.drain_metrics = drain
    return pressure
