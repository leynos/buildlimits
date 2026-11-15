"""Assemble supervisor observation ports after resource owners are constructed.

This application boundary shares cached snapshots and fixed drain counters. It
never opens resources or changes admission, launch or shutdown decisions.
"""

import contextlib
import dataclasses as dc
import typing as typ

from buildlimits.drain_metrics import DrainMetrics
from buildlimits.listener_pressure import ListenerPressure
from buildlimits.metrics_pressure import PressureMetrics, supervisor_metrics

if typ.TYPE_CHECKING:
    from buildlimits.supervisor import Supervisor


@dc.dataclass(frozen=True, slots=True)
class ObservationPorts:
    """The supervisor's pressure policy and its periodic gauge publisher.

    Attributes
    ----------
    pressure : ListenerPressure
        Listener policy sharing the supervisor's observation port.
    gauges : PressureMetrics
        Snapshot accumulator the tick, admission and shutdown paths drive.
    """

    pressure: ListenerPressure
    gauges: PressureMetrics


def configure(supervisor: Supervisor) -> ObservationPorts:
    """Wire one pressure port and one drain observer to existing collaborators.

    Parameters
    ----------
    supervisor : Supervisor
        Constructed admission, requester, group, server and loop owners.

    Returns
    -------
    ObservationPorts
        Listener policy and gauge publisher sharing the supervisor's port.
    """
    gauges = supervisor_metrics(supervisor, supervisor.metrics)
    supervisor.metrics = gauges
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
    return ObservationPorts(pressure=pressure, gauges=gauges)


def observe(supervisor: Supervisor, now: float) -> None:
    """Fold the current supervisor sample into the open interval.

    Parameters
    ----------
    supervisor : Supervisor
        Supervisor whose cached state is sampled.
    now : float
        Monotonic seconds shared with the periodic service polls.

    Notes
    -----
    Observation is best-effort. An escaping exception would count as a control
    fault in the drain loop, so a persistent telemetry fault could otherwise
    affect admission.
    """
    with contextlib.suppress(Exception):
        supervisor.gauges.observe(now)
        supervisor.gauges.flush(now)


def flush(supervisor: Supervisor, *, force: bool = False) -> None:
    """Publish the open interval, forcing the final record before shutdown.

    Parameters
    ----------
    supervisor : Supervisor
        Supervisor whose open interval is published.
    force : bool
        Whether the record is required regardless of the cadence.
    """
    with contextlib.suppress(Exception):
        now = supervisor.clock()
        supervisor.gauges.observe(now)
        supervisor.gauges.flush(now, force=force)
