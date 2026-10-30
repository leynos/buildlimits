"""Shared observation fakes for the recording-versus-null comparisons."""

import dataclasses as dc
import typing as typ

from outcome_recorder_test_helpers import stats_text
from request_helpers import crate_pending

from buildlimits.effects import ReturnToken
from buildlimits.outcome_ports import StatisticsText, Ticket

if typ.TYPE_CHECKING:
    from outcome_shell_test_support import OutcomeWorld

    from buildlimits.effects import Effect
    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.pending_request import Pending

BASELINE_POLLS = 2
"""Completed polls that still report the pre-request baseline."""


class CompletingStatistics:
    """Return deterministic cumulative statistics on every loop poll."""

    def __init__(self) -> None:
        """Start the baseline and one miss footprint sequence."""
        self.polls = 0

    def start(self, now: float) -> Ticket:
        """Start one immediate, scripted statistics query.

        Returns
        -------
        Ticket
            A ticket that becomes due two seconds after it starts.
        """
        return Ticket(self.polls + 1, now + 2.0)

    def poll(self, ticket: Ticket, now: float) -> StatisticsText:
        """Complete the query with a baseline or a single miss footprint.

        Returns
        -------
        StatisticsText
            Baseline text for the opening polls, then one miss footprint.
        """
        del ticket, now
        self.polls += 1
        if self.polls <= BASELINE_POLLS:
            return StatisticsText(stats_text())
        return StatisticsText(
            stats_text(
                compile_requests=1,
                compilations=1,
                cache_misses=1,
                requests_executed=1,
            )
        )

    @staticmethod
    def close(ticket: Ticket) -> None:
        """Accept terminal query cleanup without retaining opaque tickets."""
        del ticket


def cached_entry() -> Pending:
    """Build one safe cached compiler request for the shell's launch hook.

    Returns
    -------
    Pending
        A request whose command line exposes full-emit metadata.
    """
    entry = crate_pending(1)
    entry.cache_path = "cached"
    entry.request = dc.replace(
        entry.request,
        argv=("rustc", "--crate-name=serde", "--crate-type=lib", "lib.rs"),
        cwd="/work/serde-1.2.3",
        env={
            "CARGO_PKG_NAME": "serde",
            "CARGO_PKG_VERSION": "1.2.3",
            "CARGO_MANIFEST_DIR": "/work/serde-1.2.3",
        },
    )
    return entry


@dc.dataclass(frozen=True, slots=True)
class DifferentialObservations:
    """Authority-side trace and optional recording effects from one run."""

    input_trace: list[tuple[float, str, tuple[object, ...]]]
    dispatched_effects: list[tuple[float, str, tuple[object, ...]]]
    token_return_ticks: list[tuple[float, bytes]]
    group_states: list[object]
    metrics: list[tuple[str, dict[str, object]]]
    records: list[StoreRecord]
    pool_tokens: list[bytes]


@dc.dataclass(slots=True)
class Trace:
    """Mutable state and recorded observations for one differential run."""

    world: OutcomeWorld
    clock: list[float]
    input_trace: list[tuple[float, str, tuple[object, ...]]] = dc.field(
        default_factory=list
    )
    dispatched_effects: list[tuple[float, str, tuple[object, ...]]] = dc.field(
        default_factory=list
    )
    token_return_ticks: list[tuple[float, bytes]] = dc.field(default_factory=list)
    group_states: list[object] = dc.field(default_factory=list)
    pool_tokens: list[bytes] = dc.field(default_factory=lambda: [b"+"])

    def tick(self, now: float) -> None:
        """Advance both the observation clock and the outcome shell."""
        self.clock[0] = now
        self.world.shell.on_tick(now)

    def observe(
        self, name: str, args: tuple[object, ...], effects: list[Effect]
    ) -> list[Effect]:
        """Record one explicit admission operation and its returned effects.

        Returns
        -------
        list[Effect]
            The effects produced by the recorded operation.
        """
        self.input_trace.append((self.clock[0], name, args))
        self.dispatched_effects.append((self.clock[0], name, tuple(effects)))
        for effect in effects:
            if isinstance(effect, ReturnToken):
                self.token_return_ticks.append((self.clock[0], effect.token))
                self.pool_tokens.append(effect.token)
        return effects

    def observations(self) -> DifferentialObservations:
        """Return the recorded observations in the shared result shape.

        Returns
        -------
        DifferentialObservations
            Admission traces, token timings and optional recording outputs.
        """
        return DifferentialObservations(
            input_trace=self.input_trace,
            dispatched_effects=self.dispatched_effects,
            token_return_ticks=self.token_return_ticks,
            group_states=self.group_states,
            metrics=self.world.metrics.events,
            records=self.world.writer.records,
            pool_tokens=self.pool_tokens,
        )
