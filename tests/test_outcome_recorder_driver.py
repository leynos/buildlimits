"""Imperative query-port behaviour for the outcome recorder driver."""

import collections.abc as cabc
import typing as typ

import pytest
from outcome_recorder_test_helpers import (
    GENERATION,
    OBSERVED_AT,
    clear_gate_event,
    outcome_module,
    stats_text,
)

from buildlimits.outcome_ports import Pending, QueryFailure, StatisticsText, Ticket
from buildlimits.outcome_recorder_driver import OutcomeRecorder

if typ.TYPE_CHECKING:
    from buildlimits.outcome_attribution import Generation
    from buildlimits.outcome_recorder_types import RecorderEvent


class FakeStatistics:
    """Record the driver's bounded query operations."""

    close: cabc.Callable[[Ticket], None]

    def __init__(self, *, start_result: Ticket | QueryFailure | None = None) -> None:
        """Keep query results and calls for one driver example."""
        self.start_result = start_result
        self.started_ticket: Ticket | None = None
        self.started: list[float] = []
        self.polled: list[float] = []
        self.polled_tickets: list[Ticket] = []
        self.closed: list[Ticket] = []
        self.close = self.closed.append

    def start(self, now: float) -> Ticket | QueryFailure:
        """Return the configured result or a deliberately long ticket."""
        self.started.append(now)
        if self.start_result is not None:
            if isinstance(self.start_result, Ticket):
                self.started_ticket = self.start_result
            return self.start_result
        self.started_ticket = Ticket(serial=1, deadline=now + 100.0)
        return self.started_ticket

    def poll(
        self, ticket: Ticket, now: float
    ) -> Pending | StatisticsText | QueryFailure:
        """Return one empty statistics reading."""
        self.polled.append(now)
        self.polled_tickets.append(ticket)
        return StatisticsText(stats_text())


class FakeServer:
    """Return the fixed managed-server generation for a query."""

    def generation(self) -> Generation:
        """Return the model server identity."""
        return GENERATION

    @staticmethod
    def full_emit_metadata() -> frozenset[str]:
        """Return no compiler children for query-driver examples."""
        return frozenset()


def _started_query(now: float) -> tuple[OutcomeRecorder, FakeStatistics, Ticket]:
    """Start one bounded statistics query for a driver contract.

    Example:
        service, statistics, ticket = _started_query(1.0)
        assert statistics.started == [1.0]
        assert ticket.deadline == pytest.approx(101.0)
        assert ticket is statistics.started_ticket
    """
    recorder = outcome_module("outcome_recorder")
    statistics = FakeStatistics()
    service = OutcomeRecorder(statistics, FakeServer())
    service.advance(typ.cast("RecorderEvent", clear_gate_event(recorder, now)))
    assert isinstance(service.state, recorder.Querying), "the query is active"
    ticket = service.state.ticket
    assert ticket is not None, "an active query has a bounded ticket"
    return service, statistics, ticket


def test_driver_executes_start_and_poll_and_caps_deadline() -> None:
    """Execute query commands while returning only publishable effects."""
    recorder = outcome_module("outcome_recorder")
    statistics = FakeStatistics()
    service = OutcomeRecorder(statistics, FakeServer())

    start_output = service.advance(
        typ.cast("RecorderEvent", clear_gate_event(recorder, 3.0))
    )

    assert isinstance(service.state, recorder.Querying), "the ticket remains open"
    ticket = service.state.ticket
    assert ticket is not None, "the driver receives a ticket"
    assert statistics.started == [3.0], "the start command reaches the port"
    assert ticket.deadline == pytest.approx(103.0), "the port ticket stays unchanged"
    assert ticket is statistics.started_ticket, "the opaque ticket keeps its identity"
    assert start_output.commands == (), "the driver consumes query commands"

    result = service.poll(3.5)

    assert statistics.polled == [3.5], "the caller tick polls once"
    assert statistics.polled_tickets == [ticket], (
        "the port receives its original ticket"
    )
    assert result.reading_reasons == ("none",), "the first reading is a baseline"
    assert isinstance(service.state, recorder.Idle), "polling closes the query"
    assert service.state.progress.baseline is not None, "the baseline is retained"
    assert service.state.progress.baseline.generation == GENERATION, (
        "the server identity brackets the statistics text"
    )


def test_driver_closes_ticket_at_capped_deadline_without_an_extra_poll() -> None:
    """Apply the recorder cap without changing the port's ticket identity."""
    service, statistics, ticket = _started_query(7.0)

    output = service.poll(9.0)

    assert not statistics.polled, "an expired ticket is not polled again"
    assert statistics.closed == [ticket], "the expired ticket is closed"
    assert statistics.closed[0] is statistics.started_ticket, (
        "close receives the original opaque port ticket"
    )
    assert output.reading_reasons == ("timeout",), "the deadline has one reason"


def test_driver_honours_an_earlier_port_deadline() -> None:
    """Use the port deadline when it precedes the recorder's query cap."""
    recorder = outcome_module("outcome_recorder")
    ticket = Ticket(serial=8, deadline=3.5)
    statistics = FakeStatistics(start_result=ticket)
    service = OutcomeRecorder(statistics, FakeServer())

    service.advance(typ.cast("RecorderEvent", clear_gate_event(recorder, 3.0)))
    output = service.poll(3.5)

    assert not statistics.polled, "a port-expired query is not polled"
    assert statistics.closed == [ticket], "the expired port ticket is closed"
    assert statistics.closed[0] is ticket, "close receives the original ticket"
    assert output.reading_reasons == ("timeout",), "the port deadline is honoured"


def test_driver_reports_a_query_start_failure_without_a_ticket() -> None:
    """Treat a failed child launch as a read failure with no close command."""
    recorder = outcome_module("outcome_recorder")
    statistics = FakeStatistics(start_result=QueryFailure("read_error"))
    service = OutcomeRecorder(statistics, FakeServer())

    output = service.advance(typ.cast("RecorderEvent", clear_gate_event(recorder, 4.0)))

    assert output.reading_reasons == ("read_error",), "launch failures are named"
    assert not statistics.closed, "a failed start has no ticket to close"


def test_driver_executes_stop_close_without_publishing_outputs() -> None:
    """Close an active query when the supervisor stops."""
    service, statistics, ticket = _started_query(1.0)
    recorder = outcome_module("outcome_recorder")

    output = service.advance(recorder.Stopping(now=1.5, observed_at=OBSERVED_AT))

    assert statistics.closed == [ticket], "stop executes the close command"
    assert output.commands == (), "the caller receives no already-executed command"
