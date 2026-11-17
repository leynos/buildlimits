"""Assemble supervisor observation ports after resource owners are constructed.

This application boundary shares cached snapshots and fixed drain counters. It
never opens resources or changes admission, launch or shutdown decisions. The
gauge write is named at each call site --- :func:`sample` folds a reading,
:func:`publish_due` writes when the cadence permits, and :func:`publish_final`
writes the last interval before shutdown --- so a reader can see where records
come from.
"""

import contextlib
import dataclasses as dc
import typing as typ

from buildlimits.drain_metrics import DrainMetrics
from buildlimits.listener_pressure import ListenerPressure
from buildlimits.metrics_pressure import (
    EventForwarder,
    GaugePublisher,
    Publication,
    supervisor_publisher,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.supervisor import Supervisor


@dc.dataclass(frozen=True, slots=True)
class ObservationPorts:
    """The supervisor's pressure policy and its periodic gauge publisher.

    Attributes
    ----------
    pressure : ListenerPressure
        Listener policy sharing the supervisor's observation port.
    gauges : GaugePublisher
        Snapshot publisher the tick, admission and shutdown paths drive.
    """

    pressure: ListenerPressure
    gauges: GaugePublisher


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
    gauges = supervisor_publisher(supervisor, supervisor.metrics)
    supervisor.metrics = EventForwarder(supervisor.metrics)
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


def sample(supervisor: Supervisor, now: float) -> None:
    """Fold the current supervisor sample into the open interval.

    Parameters
    ----------
    supervisor : Supervisor
        Supervisor whose cached state is sampled.
    now : float
        Monotonic seconds shared with the periodic service polls.

    Notes
    -----
    Sampling is best-effort. An escaping exception would count as a control
    fault in the drain loop, so a persistent telemetry fault could otherwise
    affect admission.
    """
    with contextlib.suppress(Exception):
        supervisor.gauges.sample(now)


def publish_due(supervisor: Supervisor, now: float) -> Publication:
    """Write a ``gauges`` record when the cadence says one is due.

    Parameters
    ----------
    supervisor : Supervisor
        Supervisor whose open interval may be published.
    now : float
        Monotonic seconds at which the attempt runs.

    Returns
    -------
    Publication
        What the attempt achieved; an escaping fault reports
        :attr:`~buildlimits.metrics_pressure.Publication.SUPPRESSED`.
    """
    return _publish(lambda: supervisor.gauges.publish(now))


def publish_final(supervisor: Supervisor) -> Publication:
    """Write the last interval before the terminal record.

    Parameters
    ----------
    supervisor : Supervisor
        Supervisor whose remaining interval is published.

    Returns
    -------
    Publication
        What the forced attempt achieved; the caller reports it without
        letting it change the process exit status.

    Notes
    -----
    The last record is best-effort. A refusing transport must not change how
    the process exits, which is why the outcome is returned rather than raised.
    The interval is sampled at this instant first, because a record may only
    close on a reading taken at its endpoint.
    """
    now = supervisor.clock()
    sample(supervisor, now)
    return _publish(lambda: supervisor.gauges.publish(now, force=True))


def _publish(publish: cabc.Callable[[], Publication]) -> Publication:
    """Run one publication attempt behind the telemetry exception boundary.

    Parameters
    ----------
    publish : Callable[[], Publication]
        Attempt bound to the instant the caller read.

    Returns
    -------
    Publication
        The attempt's outcome, or ``SUPPRESSED`` when it raised.
    """
    with contextlib.suppress(Exception):
        return publish()
    return Publication.SUPPRESSED
