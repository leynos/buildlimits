"""Attach cached supervisor pressure observations without resource sampling.

This application adapter maps domain snapshots to the journal schema. It has
no authority over admission, descriptor ownership or listener recovery.
"""

import contextlib
import typing as typ

from buildlimits.metrics import MAX_COUNTER, gauge_fields

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.metrics import MetricsPort
    from buildlimits.supervisor import Supervisor


class PressureMetrics:
    """Add cached gauges and a lifetime queue maximum to an injected port.

    Parameters
    ----------
    port : MetricsPort
        Existing validating or recording emitter.
    snapshot : Callable[[], Mapping[str, object]]
        Reads cached producer state only; failures discard the observation.
    """

    def __init__(
        self,
        port: MetricsPort,
        snapshot: cabc.Callable[[], cabc.Mapping[str, object]],
    ) -> None:
        """Bind the observer and reset its process-lifetime queue maximum."""
        self._port = port
        self._snapshot = snapshot
        self._queued_high_water = 0

    def emit(self, event: str, **fields: object) -> None:
        """Emit one enriched event without allowing observation errors out.

        Parameters
        ----------
        event : str
            Existing fixed catalogue event.
        **fields : object
            Event-specific bounded measurements.
        """
        with contextlib.suppress(Exception):
            gauges = dict(self._snapshot())
            queued = gauges.get("queued")
            if isinstance(queued, int) and not isinstance(queued, bool):
                self._queued_high_water = min(
                    MAX_COUNTER, max(self._queued_high_water, queued)
                )
            fields.update(gauges)
            fields["queued_high_water"] = self._queued_high_water
            self._port.emit(event, **fields)


def supervisor_metrics(supervisor: Supervisor, port: MetricsPort) -> PressureMetrics:
    """Bind delayed cached-state reads after supervisor construction completes.

    Parameters
    ----------
    supervisor : Supervisor
        Owner of admission, request-budget and listener state.
    port : MetricsPort
        Caller-supplied emission boundary.

    Returns
    -------
    PressureMetrics
        Observer that neither samples resources nor changes domain state.
    """
    return PressureMetrics(port, lambda: _snapshot(supervisor))


def _snapshot(supervisor: Supervisor) -> dict[str, object]:
    """Read only cached domain and application observations.

    Returns
    -------
    dict[str, object]
        Fixed gauge names mapped to cached producer measurements.
    """
    limits = supervisor.requests.descriptors
    return {
        **gauge_fields(supervisor.admission.counts()),
        "fd_retained": supervisor.requests.retained,
        "fd_budget": limits.budget,
        "fd_high_water": limits.high_water,
        "fd_budget_known": limits.baseline_known,
        "accept_paused": supervisor.pressure.paused,
    }
