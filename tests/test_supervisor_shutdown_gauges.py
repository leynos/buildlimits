"""The forced final gauge record and the terminal record keep their order.

A reader that sums ``gauges`` intervals must see the interval that ends at
shutdown. That record therefore precedes ``supervisor_stopped``, and its
publication outcome is reported without changing the process exit status.
"""

import dataclasses as dc
import typing as typ

from supervisor_helpers import one_slot_supervisor

from buildlimits import supervisor_observability, supervisor_shutdown
from buildlimits.metrics import Metrics

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest
    from conftest import RecordingSink

    from buildlimits.metrics import MetricsPort


class _RefusingGauges:
    """Forward every event except ``gauges``, which the transport refuses."""

    def __init__(self, inner: MetricsPort) -> None:
        """Hold the port every non-gauge event is forwarded to."""
        self._inner = inner

    def emit(self, event: str, **fields: object) -> bool:
        """Refuse the gauge record and forward every other event.

        Returns
        -------
        bool
            ``False`` for ``gauges``; otherwise the inner port's outcome.
        """
        if event == "gauges":
            return False
        return self._inner.emit(event, **fields)


def _refusing_port(sink: RecordingSink) -> MetricsPort:
    """Build the real validating emitter behind a transport that drops gauges.

    Returns
    -------
    MetricsPort
        The refusing boundary over the scenario's recording sink.
    """
    return _RefusingGauges(Metrics(sink))


@dc.dataclass(frozen=True, slots=True)
class _Outcome:
    """What one shutdown wrote and reported.

    Attributes
    ----------
    records : list[dict[str, str]]
        Every record the scenario's sink accepted, in order.
    logged : list[str]
        The routine lines the shutdown path wrote.
    exit_status : int | None
        The status the supervisor left behind for its process to exit with.
    baseline_written : bool
        Whether the transport accepted the interval that anchors the run.
    """

    records: list[dict[str, str]]
    logged: list[str]
    exit_status: int | None
    baseline_written: bool


def _shutdown_after_a_baseline(
    monkeypatch: pytest.MonkeyPatch,
    port_factory: cabc.Callable[[RecordingSink], MetricsPort] = Metrics,
) -> _Outcome:
    """Anchor an interval at zero, then shut down fifteen seconds later.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Redirects the shutdown path's routine log.
    port_factory : Callable[[RecordingSink], MetricsPort]
        Builds the emission boundary; the default is the real emitter.

    Returns
    -------
    _Outcome
        The records, routine lines and exit status the shutdown produced.
    """
    logged: list[str] = []
    monkeypatch.setattr(supervisor_shutdown, "log", logged.append)
    with one_slot_supervisor(port_factory) as scenario:
        supervisor = scenario.supervisor
        supervisor_observability.sample(supervisor, 0.0)
        baseline_written = supervisor_observability.publish_due(
            supervisor, 0.0
        ).is_written
        scenario.clock.time = 15.0
        supervisor._stopping = True
        supervisor._shutdown()
        return _Outcome(
            records=[dict(record) for record in scenario.sink.records],
            logged=logged,
            exit_status=supervisor.exit_status,
            baseline_written=baseline_written,
        )


def test_the_final_interval_is_written_before_the_terminal_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown publishes the whole interval it observed, then announces itself."""
    outcome = _shutdown_after_a_baseline(monkeypatch)
    assert outcome.baseline_written, "the baseline record anchors the interval at zero"

    events = [record["BUILD_LIMITS_EVENT"] for record in outcome.records]
    stopped_at = events.index("supervisor_stopped")
    final = outcome.records[stopped_at - 1]
    assert final["BUILD_LIMITS_EVENT"] == "gauges", (
        "the last interval reaches the journal before the terminal record"
    )
    assert final["BUILD_LIMITS_INTERVAL_SECONDS"] == "15.000", (
        "the forced record covers the time since the baseline"
    )
    assert outcome.logged == ["stopping: final gauge record written"], (
        "the shutdown path reports the outcome it achieved"
    )


def test_a_refused_final_record_is_reported_without_changing_the_exit_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing transport is visible in the log, not in the process status."""
    outcome = _shutdown_after_a_baseline(monkeypatch, _refusing_port)
    assert not outcome.baseline_written, "the transport refuses the baseline"
    assert outcome.exit_status == 0, (
        "a refused telemetry record cannot change the exit status"
    )
    assert outcome.logged == ["stopping: final gauge record refused"], (
        "the operator learns the last interval was lost"
    )
    assert [record["BUILD_LIMITS_EVENT"] for record in outcome.records] == [
        "supervisor_stopped"
    ], "the terminal record is still written and no gauge record pretends otherwise"


def test_a_failing_diagnostic_write_still_writes_the_terminal_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken stderr cannot keep the terminal record out of the journal."""

    def _refuse(_line: str) -> None:
        """Fail every diagnostic write the way a closed stderr does."""
        raise BrokenPipeError

    monkeypatch.setattr(supervisor_shutdown, "log", _refuse)
    with one_slot_supervisor() as scenario:
        supervisor = scenario.supervisor
        supervisor._stopping = True
        supervisor._shutdown()

    events = [record["BUILD_LIMITS_EVENT"] for record in scenario.sink.records]
    assert events[-1] == "supervisor_stopped", (
        "the terminal record follows the diagnostic even when stderr fails"
    )
