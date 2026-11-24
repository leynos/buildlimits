"""Imperative driver for recorder query commands."""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_ports import (
    Pending,
    ServerProcessPort,
    StatisticsPort,
    StatisticsText,
)
from buildlimits.outcome_recorder import OUTCOME_QUERY_SECONDS, advance
from buildlimits.outcome_recorder_effects import _join_outputs
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

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt


class OutcomeRecorder:
    """Thin statistics-port driver that returns effects to the caller."""

    def __init__(
        self,
        statistics: StatisticsPort,
        server: ServerProcessPort,
        *,
        wall_clock: cabc.Callable[[], dt.datetime] | None = None,
    ) -> None:
        """Create an enabled driver around the query and generation ports.

        ``wall_clock`` reads the wall-clock moment a port result became known.
        The supervisor shell supplies its own clock, so a reading's evidence
        and the recorder share one time source. A caller that supplies none
        leaves the row's observation time to the documented synthetic
        fallback.

        Examples
        --------
        A new driver starts in the ``Idle`` state.
        """
        self._statistics = statistics
        self._server = server
        self._wall_clock = wall_clock
        self.state: RecorderState = Idle()

    def _observed_at(self) -> dt.datetime | None:
        """Return the moment the last port result became known.

        Returns
        -------
        datetime or None
            The shell's wall clock, or ``None`` when no clock was supplied.
        """
        return None if self._wall_clock is None else self._wall_clock()

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
        # The port has answered, so this is the moment a terminal result's
        # labels or refusals became known.
        observed_at = self._observed_at()
        after = (
            self._server.generation() if isinstance(result, StatisticsText) else None
        )
        return self.advance(
            PollResult(
                now=now,
                result=result,
                after_generation=after,
                observed_at=observed_at,
            )
        )

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
                            observed_at=self._observed_at(),
                        ),
                    )
                    self.state = state
                    combined = _join_outputs(combined, self._execute(response))
                case CloseQuery(ticket=ticket):
                    self._statistics.close(ticket)
        return combined
