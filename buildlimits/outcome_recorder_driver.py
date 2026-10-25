"""Imperative driver for recorder query commands."""

import dataclasses as dc

from buildlimits.outcome_ports import (
    Pending,
    ServerProcessPort,
    StatisticsPort,
    StatisticsText,
)
from buildlimits.outcome_recorder import OUTCOME_QUERY_SECONDS, advance
from buildlimits.outcome_recorder_outputs import _join_outputs
from buildlimits.outcome_recorder_types import (
    CloseQuery,
    Idle,
    Outputs,
    PollResult,
    Querying,
    RecorderEvent,
    RecorderState,
    StartQuery,
)


class OutcomeRecorder:
    """Thin statistics-port driver that returns effects to the caller."""

    def __init__(self, statistics: StatisticsPort, server: ServerProcessPort) -> None:
        """Create an enabled driver around the query and generation ports.

        Examples
        --------
        A new driver starts in the ``Idle`` state.
        """
        self._statistics = statistics
        self._server = server
        self.state: RecorderState = Idle()

    def advance(self, event: RecorderEvent) -> Outputs:
        """Apply an event and execute only the returned query commands.

        Returns
        -------
        Outputs
            Events and records for the caller to publish, without commands.

        Examples
        --------
        ``OutcomeRecorder(stats, server).advance(LaunchFailed("g"))`` returns
        empty output.
        """
        self.state, outputs = advance(self.state, event)
        return self._execute(outputs)

    def poll(self, now: float) -> Outputs:
        """Poll the current ticket once and feed its result back to the reducer.

        Returns
        -------
        Outputs
            The completed reading effects, or empty output while no ticket exists.

        Examples
        --------
        Polling an expired ticket returns a ``"timeout"`` reading reason.
        """
        if not isinstance(self.state, Querying) or self.state.ticket is None:
            return Outputs()
        deadline = min(
            self.state.ticket.deadline,
            self.state.started_at + OUTCOME_QUERY_SECONDS,
        )
        result = (
            Pending()
            if now >= deadline
            else self._statistics.poll(self.state.ticket, now)
        )
        after = (
            self._server.generation() if isinstance(result, StatisticsText) else None
        )
        return self.advance(PollResult(now=now, result=result, after_generation=after))

    def _execute(self, outputs: Outputs) -> Outputs:
        """Execute port commands while leaving publication to the caller.

        Returns
        -------
        Outputs
            The input effects with executed commands removed.

        Examples
        --------
        Executing an empty output returns ``Outputs()``.
        """
        combined = dc.replace(outputs, commands=())
        for command in outputs.commands:
            match command:
                case StartQuery(now=now):
                    before = self._server.generation()
                    result = self._statistics.start(now)
                    state, response = advance(
                        self.state,
                        PollResult(
                            now=now,
                            result=result,
                            before_generation=before,
                        ),
                    )
                    self.state = state
                    combined = _join_outputs(combined, self._execute(response))
                case CloseQuery(ticket=ticket):
                    self._statistics.close(ticket)
        return combined
