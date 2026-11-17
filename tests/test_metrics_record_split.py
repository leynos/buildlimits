"""The producer-side split between per-request and supervisor-wide fields.

A per-request record describes one request. The counters that describe the
whole supervisor reach the journal on the periodic ``gauges`` snapshot and on
rare transition records, so `metrics_schema.SUPERVISOR_WIDE_FIELDS` must never
appear on an admission, a terminal result or a pre-registration refusal.
"""

import typing as typ

from request_helpers import crate_pending
from supervisor_helpers import supervisor

from buildlimits import effect_dispatch
from buildlimits.metrics import SUPERVISOR_WIDE_FIELDS
from buildlimits.protocol import Exit

if typ.TYPE_CHECKING:
    from buildlimits.supervisor import Supervisor

__all__ = ["supervisor"]  # Re-exported so pytest finds the fixture here.


def test_per_request_records_carry_no_supervisor_wide_fields(
    supervisor: Supervisor, metrics, metric_sink
) -> None:
    """The split is enforced by every producer of a per-request record."""
    supervisor.metrics = metrics
    supervisor.requests.metrics = metrics
    entry = crate_pending(1)
    supervisor.requests.pending[1] = entry
    effect_dispatch.record_admitted(supervisor, entry, cached=False)
    supervisor.requests.finish(1, Exit(code=0))
    supervisor.requests._rejected("busy")
    assert [r["BUILD_LIMITS_EVENT"] for r in metric_sink.records] == [
        "request_admitted",
        "request_finished",
        "request_finished",
    ], "admission, a terminal result and a pre-registration refusal are covered"
    produced = {
        name.removeprefix("BUILD_LIMITS_").lower()
        for record in metric_sink.records
        for name in record
    }
    assert not produced & SUPERVISOR_WIDE_FIELDS, (
        "a per-request record must not duplicate the periodic snapshot"
    )
