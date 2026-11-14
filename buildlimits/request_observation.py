"""Where a finished request's outcome is reported and how much is written."""

import dataclasses
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.admission import Counts
    from buildlimits.journal_policy import RoutineLineWriter
    from buildlimits.metrics import MetricsPort


@dataclasses.dataclass(frozen=True, slots=True)
class RequestObservation:
    """The observation points one request table reports to.

    Parameters
    ----------
    metrics : MetricsPort | None
        Injected failure-isolated metric emitter; absent uses the default
        transport.
    counts : Callable[[], Counts] | None
        Reads current admission gauges for each terminal record.
    lines : RoutineLineWriter | None
        The supervisor's journal writer, holding the resolved setting and the
        stream it governs; absent writes no routine line, which is the
        operator default.
    """

    metrics: MetricsPort | None = None
    counts: cabc.Callable[[], Counts] | None = None
    lines: RoutineLineWriter | None = None
