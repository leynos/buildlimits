"""Small deterministic ports for supervisor outcome-shell tests."""

import dataclasses as dc
import typing as typ
from types import SimpleNamespace

from buildlimits.admission import Admission
from buildlimits.metrics import Metrics
from buildlimits.outcome_attribution import Generation
from buildlimits.outcome_key import Salt
from buildlimits.outcome_ports import Pending, StatisticsText, StoreResult, Ticket
from buildlimits.supervisor_outcome_setup import NullOutcomeShell, OutcomeShellPort
from buildlimits.supervisor_outcomes import (
    OutcomeDependencies,
    OutcomeShell,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.nonblocking_child import ChildReaper
    from buildlimits.outcome_ports import (
        OutcomeStorePort,
        ServerProcessPort,
        StatisticsPort,
    )
    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.supervisor import Supervisor
    from buildlimits.tracing import JournalTracer


class RecordingMetrics(Metrics):
    """Retain submitted event values while preserving schema validation."""

    def __init__(self) -> None:
        """Start an empty bounded observation list."""
        super().__init__()
        self.events: list[tuple[str, dict[str, object]]] = []

    def emit(self, event: str, **fields: object) -> None:
        """Record an event for assertions, then use the real schema boundary."""
        self.events.append((event, fields))
        super().emit(event, **fields)


class PendingStatistics:
    """Return a stable query ticket whose poll remains pending."""

    def __init__(self, poll_result: Pending | StatisticsText | None = None) -> None:
        """Create call records for a pending or immediate query result."""
        self.starts: list[float] = []
        self.polls: list[float] = []
        self.issued: list[Ticket] = []
        self.closed: list[Ticket] = []
        self.poll_result = Pending() if poll_result is None else poll_result

    def start(self, now: float) -> Ticket:
        """Start one pending query with a two-second deadline.

        Returns
        -------
        Ticket
            The opaque ticket that remains pending until its deadline.
        """
        self.starts.append(now)
        ticket = Ticket(len(self.starts), now + 2.0)
        self.issued.append(ticket)
        return ticket

    def poll(self, ticket: Ticket, now: float) -> Pending | StatisticsText:
        """Keep the scripted ticket outstanding without waiting.

        Returns
        -------
        Pending | StatisticsText
            The configured pending state or immediate statistics document.
        """
        del ticket
        self.polls.append(now)
        return self.poll_result

    def close(self, ticket: Ticket) -> None:
        """Record cancellation of an outstanding query.

        Raises
        ------
        AssertionError
            If the ticket was not issued by this statistics port.
        """
        if ticket not in self.issued:
            raise AssertionError
        self.closed.append(ticket)


class FakeServerProcess:
    """Supply one stable identity for generation-bound recorder tests."""

    @staticmethod
    def generation() -> Generation:
        """Return a fixed server identity.

        Returns
        -------
        Generation
            One stable test server identity.
        """
        return Generation("invocation", 123, 456)

    @staticmethod
    def full_emit_metadata() -> frozenset[str]:
        """Return no child metadata in query-only recorder tests.

        Returns
        -------
        frozenset[str]
            No fake compiler metadata.
        """
        return frozenset()


class FakeWriter:
    """Accept records and expose deterministic startup and close results."""

    def __init__(self) -> None:
        """Create empty store call records."""
        self.records: list[StoreRecord] = []
        self.closed: list[float] = []
        self.is_closed = False

    @staticmethod
    def salt_result() -> Salt:
        """Return test-only stable key material immediately.

        Returns
        -------
        Salt
            Fixed local-only key material for the recorder tests.
        """
        return Salt(b"s" * 32, "test-salt")

    @staticmethod
    def results() -> tuple[StoreResult, ...]:
        """Return no asynchronous store work by default.

        Returns
        -------
        tuple[StoreResult, ...]
            An empty result collection.
        """
        return ()

    def append(self, record: StoreRecord) -> None:
        """Retain an immutable record for the surrounding test.

        Raises
        ------
        AssertionError
            If an append is attempted after shutdown.
        """
        if self.is_closed:
            raise AssertionError
        self.records.append(record)

    def close(self, timeout: float) -> None:
        """Record the caller's bounded shutdown allowance.

        Raises
        ------
        AssertionError
            If the shutdown timeout is negative.
        """
        if timeout < 0:
            raise AssertionError
        self.closed.append(timeout)
        self.is_closed = True


@dc.dataclass(slots=True)
class OutcomeWorld:
    """Bundle the shell, its actual admission machine and observation fakes."""

    shell: OutcomeShellPort
    admission: Admission
    metrics: RecordingMetrics
    statistics: PendingStatistics
    writer: FakeWriter


@dc.dataclass(frozen=True, slots=True)
class OutcomeWorldOptions:
    """Optional injected ports and quiet-point settings for shell tests."""

    recovery_marker: Path | None = None
    recording: bool = True
    statistics_port: StatisticsPort | None = None
    reaper: ChildReaper | None = None
    writer: OutcomeStorePort | None = None
    clock: cabc.Callable[[], float] | None = None
    tracer: JournalTracer | None = None


_DEFAULT_OUTCOME_WORLD_OPTIONS = OutcomeWorldOptions()


def make_outcome_world(
    tmp_path: Path,
    options: OutcomeWorldOptions = _DEFAULT_OUTCOME_WORLD_OPTIONS,
) -> OutcomeWorld:
    """Create a record-mode shell with an idle quiet point and fake ports.

    Example:
        world = make_outcome_world(tmp_path)
        world.shell.on_tick(0.0)

    Returns
    -------
    OutcomeWorld
        A shell and recording fakes with an idle quiet point.
    """
    admission = Admission(cached=True, capacity=2)
    metrics = RecordingMetrics()
    selected_statistics = (
        PendingStatistics()
        if options.statistics_port is None
        else options.statistics_port
    )
    writer = FakeWriter() if options.writer is None else options.writer
    supervisor = SimpleNamespace(
        settings=SimpleNamespace(
            sccache_socket=tmp_path / "sccache.sock",
            sccache_executable="sccache",
            recovery_marker=(
                tmp_path / "barrier"
                if options.recovery_marker is None
                else options.recovery_marker
            ),
            cache_outcomes="record" if options.recording else "off",
        ),
        metrics=metrics,
        tracer=options.tracer,
        clock=lambda: 0.0,
        admission=admission,
        server=SimpleNamespace(cache_allowed=lambda: True),
        recovery=SimpleNamespace(describe=lambda _now: {"phase": "idle"}),
    )
    supervisor.clock = (lambda: 0.0) if options.clock is None else options.clock
    if options.recording:
        shell = OutcomeShell(
            typ.cast("Supervisor", supervisor),
            dependencies=OutcomeDependencies(
                environment={"HOME": str(tmp_path)},
                writer=writer,
                statistics=selected_statistics,
                server=typ.cast("ServerProcessPort", FakeServerProcess()),
                reaper=options.reaper,
            ),
        )
    else:
        shell = NullOutcomeShell()
    return OutcomeWorld(
        shell,
        admission,
        metrics,
        typ.cast("PendingStatistics", selected_statistics),
        # An injected writer is cast to the default fake only so callers that
        # replace it own the concrete type they passed.
        typ.cast("FakeWriter", writer),
    )
