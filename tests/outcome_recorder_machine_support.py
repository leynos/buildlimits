"""Fake statistics-port sequences for the recorder state machine."""

import typing as typ

from outcome_recorder_test_helpers import OBSERVED_AT, recorder_progress, stats_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from types import ModuleType

TRUTHS = ("hit", "miss", "not_cached", "declined")
REQUIRED_CASES = {
    "solo",
    "unanimous",
    "mixed",
    "rejected",
    "abandonment",
    "straddle",
    "restart",
    "overflow",
}


class _Outcome(typ.Protocol):
    """The output fields checked against model-server truth."""

    cache_result: str
    outcome_attribution: str
    outcome_reason: str


class _Ticket(typ.Protocol):
    """Deadline carried by a fake statistics-port ticket."""

    deadline: float


class _Output(typ.Protocol):
    """Output fields used by the fake-port assertions."""

    commands: tuple[object, ...]
    events: tuple[_Outcome, ...]
    reading_reasons: tuple[str, ...]


class _RecorderMachine(typ.Protocol):
    """State and reducer operations owned by the Hypothesis machine."""

    recorder: ModuleType
    ports: ModuleType
    gate: ModuleType
    attribution: ModuleType
    state: object
    now: float
    generation: object
    server_counters: dict[str, int]
    server_drain_count: int
    ticket_serial: int
    active: dict[str, str | None]

    def _apply(self, recorder_event: object) -> _Output:
        """Apply one reducer event and return its outputs."""
        ...

    def _launch(self, truth: str | None = None, *, cached: bool = True) -> str:
        """Add a request to the recorder and model server."""
        ...

    def _expect(
        self,
        group_id: str,
        cache_result: str,
        reason: str,
        attribution: str | None = None,
    ) -> None:
        """Check one emitted outcome against its expected disposition."""
        ...

    def _mark(self, name: str) -> None:
        """Record a non-vacuity label for the current machine example."""
        ...


class RecorderReadings:
    """Drive fake query tickets from model-server counters and outcomes."""

    def __init__(self, machine: _RecorderMachine) -> None:
        """Keep access to the state machine's model and reducer."""
        self.machine = machine

    def record_server_outcome(self, truth: str) -> None:
        """Advance cumulative counters from one model request result.

        Raises
        ------
        AssertionError
            If ``truth`` is outside the model's supported outcomes.

        Examples
        --------
        ``record_server_outcome("hit")`` advances the hit counters once.
        """
        fields_by_truth = {
            "hit": ("compile_requests", "requests_executed", "cache_hits"),
            "miss": (
                "compile_requests",
                "requests_executed",
                "compilations",
                "cache_misses",
            ),
            "not_cached": (
                "compile_requests",
                "requests_executed",
                "compilations",
            ),
            "declined": ("compile_requests", "requests_not_cacheable"),
        }
        fields = fields_by_truth.get(truth)
        if fields is None:
            raise AssertionError
        for field in fields:
            counters = self.machine.server_counters
            counters[field] = counters.get(field, 0) + 1

    def finish(self, group_id: str, *, is_clean: bool) -> None:
        """End a group and make its server result visible to readings."""
        truth = self.machine.active.pop(group_id)
        if truth is not None:
            self.record_server_outcome(truth)
        self.machine.now += 0.1
        self.machine._apply(
            self.machine.recorder.Ended(
                group_id=group_id,
                is_clean=is_clean,
                now=self.machine.now,
                observed_at=OBSERVED_AT,
            )
        )

    def active_cached(self) -> tuple[str, ...]:
        """Return active cached requests for the quiet-point model.

        Returns
        -------
        tuple[str, ...]
            Active cached request identifiers.

        Examples
        --------
        ``active_cached()`` is ``()`` when no cached group is running.
        """
        return tuple(
            group for group, truth in self.machine.active.items() if truth is not None
        )

    def can_run_special_read(self) -> bool:
        """Whether a generated scenario can read outside a cached launch.

        Returns
        -------
        bool
            Whether recording is enabled and no cached request is moving.

        Examples
        --------
        ``can_run_special_read()`` is ``False`` while a cached request runs.
        """
        return (
            not isinstance(self.machine.state, self.machine.recorder.Disabled)
            and not self.active_cached()
        )

    def _gate_event(self) -> object:
        """Build a gate event from drain and cached-movement counts.

        Returns
        -------
        object
            A gate-open event with the current fake admission snapshot.

        Examples
        --------
        ``_gate_event()`` carries the current model's server-drain count.
        """
        from buildlimits.admission_state import Counts

        machine = self.machine
        running = len(machine.active)
        held = running + machine.server_drain_count
        cached_moving = len(self.active_cached())
        inputs = machine.gate.QuietInputs(
            counts=Counts(
                0,
                held,
                running,
                0,
                machine.server_drain_count,
                max(1, held),
                cached_moving,
            ),
            is_reachable=True,
            is_cache_allowed=True,
            is_recovery_idle=True,
            has_barrier=False,
            has_query_in_flight=isinstance(machine.state, machine.recorder.Querying),
        )
        return machine.recorder.GateOpen(inputs=inputs, now=machine.now)

    def start_query(self) -> bool:
        """Open a fake ticket only after the model gate reaches a quiet point.

        Returns
        -------
        bool
            Whether the reducer now holds an outstanding query ticket.

        Examples
        --------
        ``start_query()`` is ``True`` after a clear gate accepts a ticket.
        """
        machine = self.machine
        if isinstance(machine.state, machine.recorder.Disabled):
            return False
        if isinstance(machine.state, machine.recorder.Querying):
            ticket: _Ticket | None = getattr(machine.state, "ticket", None)
            if ticket is not None:
                return True
        elif self.active_cached():
            return False
        machine.now = max(
            machine.now + 0.1,
            recorder_progress(machine.state).next_read_at,
        )
        machine._apply(self._gate_event())
        if not isinstance(machine.state, machine.recorder.Querying):
            return False
        ticket = getattr(machine.state, "ticket", None)
        if ticket is None:
            machine.ticket_serial += 1
            deadline = machine.now + 2.0
            machine._apply(
                machine.recorder.PollResult(
                    now=machine.now,
                    result=machine.ports.Ticket(
                        serial=machine.ticket_serial, deadline=deadline
                    ),
                    before_generation=machine.generation,
                )
            )
        return getattr(machine.state, "ticket", None) is not None

    def finish_read(
        self,
        *,
        delay: float = 0.1,
        failure: typ.Literal["timeout", "read_error"] | None = None,
    ) -> _Output | None:
        """Complete a fake ticket with counters or one port failure.

        Returns
        -------
        _Output or None
            Reducer output when a ticket completes, or ``None`` when the gate
            cannot start a query.

        Examples
        --------
        ``finish_read(failure="timeout")`` returns the timeout output.
        """
        machine = self.machine
        if not self.start_query():
            return None
        ticket: _Ticket | None = getattr(machine.state, "ticket", None)
        if ticket is None:
            return None
        if failure == "timeout":
            machine.now = max(machine.now + delay, ticket.deadline + 0.1)
            result = machine.ports.QueryFailure("timeout")
        elif failure is not None:
            machine.now += delay
            result = machine.ports.QueryFailure(failure)
        else:
            machine.now += delay
            result = machine.ports.StatisticsText(stats_text(**machine.server_counters))
        return machine._apply(
            machine.recorder.PollResult(
                now=machine.now,
                result=result,
                after_generation=machine.generation,
            )
        )

    def exercise_balanced_windows(self) -> None:
        """Reach the no-baseline, solo, unanimous and mixed outcomes.

        Examples
        --------
        ``exercise_balanced_windows()`` records all four required cases.
        """
        machine = self.machine
        direct_group = machine._launch(cached=False)
        self.finish(direct_group, is_clean=True)

        no_baseline_group = machine._launch("hit")
        self.finish(no_baseline_group, is_clean=True)
        self.finish_read()
        machine._expect(no_baseline_group, "unattributed", "no_baseline")

        solo, _ = self.window(machine._launch, self.finish, ("hit",))
        machine._expect(solo[0], "hit", "none", "solo")
        machine._mark("solo")

        unanimous, _ = self.window(machine._launch, self.finish, ("hit", "hit"))
        for group_id in unanimous:
            machine._expect(group_id, "hit", "none", "unanimous")
        machine._mark("unanimous")

        mixed, _ = self.window(machine._launch, self.finish, ("hit", "miss"))
        for group_id in mixed:
            machine._expect(group_id, "unattributed", "mixed_window")
        machine._mark("mixed")

    def window(
        self,
        launch: cabc.Callable[[str], str],
        finish: cabc.Callable[..., None],
        truths: tuple[str, ...],
    ) -> tuple[list[str], _Output | None]:
        """Run one completed cached window and close it with a model reading.

        Returns
        -------
        tuple[list[str], _Output or None]
            The request identifiers and the closing reading output.

        Examples
        --------
        ``window(launch, finish, ("hit",))`` returns one request identifier.
        """
        group_ids = [launch(truth) for truth in truths]
        for group_id in group_ids:
            finish(group_id, is_clean=True)
        return group_ids, self.finish_read()

    def enter_server_drain(self) -> _Output:
        """Present SERVER_DRAIN without opening a query.

        Returns
        -------
        _Output
            Gate output that contains no query command.

        Examples
        --------
        ``enter_server_drain().commands`` is empty while the drain is held.
        """
        self.machine.server_drain_count += 1
        self.machine.now += 0.1
        return self.machine._apply(self._gate_event())
