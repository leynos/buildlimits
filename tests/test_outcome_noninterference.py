"""Admission traces stay identical with the recording shell on or off."""

import ast
import dataclasses as dc
import typing as typ
from pathlib import Path

from outcome_recorder_test_helpers import stats_text
from outcome_shell_test_support import OutcomeWorldOptions, make_outcome_world
from request_helpers import crate_pending

from buildlimits.admission import Launch
from buildlimits.effects import Exit, ReturnToken
from buildlimits.outcome_ports import StatisticsText, Ticket
from buildlimits.outcome_recorder_types import Querying
from buildlimits.supervisor_outcome_setup import GroupEnding

ROOT = Path(__file__).parents[1]
_ADMISSION_MODULES = (
    "admission.py",
    "admission_state.py",
    "effect_dispatch.py",
    "groups.py",
    "group_drain.py",
    "cache_recovery.py",
)

if typ.TYPE_CHECKING:
    from buildlimits.effects import Effect
    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.pending_request import Pending
    from buildlimits.supervisor_outcomes import OutcomeShell


@dc.dataclass(frozen=True, slots=True)
class _DifferentialObservations:
    """Authority-side trace and optional recording effects from one run."""

    input_trace: list[tuple[float, str, tuple[object, ...]]]
    dispatched_effects: list[tuple[float, str, tuple[object, ...]]]
    token_return_ticks: list[tuple[float, bytes]]
    group_states: list[object]
    metrics: list[tuple[str, dict[str, object]]]
    records: list[StoreRecord]
    pool_tokens: list[bytes]


class CompletingStatistics:
    """Return deterministic cumulative statistics on every loop poll."""

    def __init__(self) -> None:
        """Start the baseline and one miss footprint sequence."""
        self.polls = 0

    def start(self, now: float) -> Ticket:
        """Start one immediate, scripted statistics query."""
        return Ticket(self.polls + 1, now + 2.0)

    def poll(self, ticket: Ticket, now: float) -> StatisticsText:
        """Complete the query with a baseline or a single miss footprint."""
        del ticket, now
        self.polls += 1
        if self.polls < 3:
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


def _cached_entry() -> Pending:
    """Build one safe cached compiler request for the shell's launch hook."""
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


def _delay_query_for_token(
    shell: OutcomeShell, *, is_enabled: bool, now: float, clock: list[float]
) -> None:
    """Delay one pending query so the comparison changes token timing."""
    if is_enabled and isinstance(shell._recorder.state, Querying):
        clock[0] = now
        shell.on_tick(now)


def _differential_run(
    tmp_path: Path, *, recording: bool, delay_token: bool = False
) -> _DifferentialObservations:
    """Drive one cache-group trace and return its authority-side observations.

    Returns
    -------
    _DifferentialObservations
        Admission traces, token timings and optional recording outputs.
    """
    clock = [0.0]
    statistics = CompletingStatistics()
    world = make_outcome_world(
        tmp_path,
        OutcomeWorldOptions(
            recording=recording,
            statistics_port=statistics,
            clock=lambda: clock[0],
        ),
    )
    admission = world.admission
    shell = world.shell
    input_trace: list[tuple[float, str, tuple[object, ...]]] = []
    dispatched_effects: list[tuple[float, str, tuple[object, ...]]] = []
    returned_tokens: list[tuple[float, bytes]] = []
    pool_tokens = [b"+"]
    group_states: list[object] = []

    def tick(now: float) -> None:
        """Advance both the observation clock and the outcome shell."""
        clock[0] = now
        shell.on_tick(now)

    def observe(
        name: str, args: tuple[object, ...], effects: list[Effect]
    ) -> list[Effect]:
        """Record one explicit admission operation and its returned effects.

        Returns
        -------
        list[Effect]
            The effects produced by the recorded operation.
        """
        input_trace.append((clock[0], name, args))
        dispatched_effects.append((clock[0], name, tuple(effects)))
        for effect in effects:
            if isinstance(effect, ReturnToken):
                returned_tokens.append((clock[0], effect.token))
                pool_tokens.append(effect.token)
        return effects

    tick(0.0)
    tick(0.1)
    tick(1.1)
    if recording:
        assert isinstance(typ.cast("OutcomeShell", shell)._recorder.state, Querying), (
            '_differential_run: isinstance(typ.cast("OutcomeShell", shell)._recor'
        )
    observe("submit", (1,), admission.submit(1))

    _delay_query_for_token(
        typ.cast("OutcomeShell", shell),
        is_enabled=delay_token and recording,
        now=1.2,
        clock=clock,
    )

    token = pool_tokens.pop(0)
    effects = observe("token_arrived", (token,), admission.token_arrived(token))
    launch = next(effect for effect in effects if isinstance(effect, Launch))
    shell.launched(_cached_entry(), launch.group_id)
    observe(
        "leader_exited",
        (launch.group_id, Exit(signal=9)),
        admission.leader_exited(launch.group_id, Exit(signal=9)),
    )
    empty_effects = observe(
        "group_emptied",
        (launch.group_id,),
        admission.group_emptied(launch.group_id),
    )
    group_states.append(admission.groups[launch.group_id].state.value)
    shell.emptied(GroupEnding(launch.group_id, True, False))
    assert not any(isinstance(effect, ReturnToken) for effect in empty_effects), (
        "_differential_run: not any(isinstance(effect, ReturnToken) for effec"
    )
    observe("server_activity", (0,), admission.server_activity(0))

    if clock[0] < 1.2:
        tick(1.2)
    tick(2.3)
    tick(2.4)

    return _DifferentialObservations(
        input_trace=input_trace,
        dispatched_effects=dispatched_effects,
        token_return_ticks=returned_tokens,
        group_states=group_states,
        metrics=world.metrics.events,
        records=world.writer.records,
        pool_tokens=pool_tokens,
    )


def test_decision_modules_do_not_import_outcome_recording() -> None:
    """Admission decisions remain independent of optional recording code."""
    package = ROOT / "buildlimits"
    for filename in _ADMISSION_MODULES:
        tree = ast.parse((package / filename).read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        } | {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert not any(name.startswith("outcome_") for name in imported), filename


def test_outcome_hooks_follow_the_admission_effects() -> None:
    """The two integration hooks run after release and empty accounting."""
    placement = (ROOT / "buildlimits/supervisor_placement.py").read_text(
        encoding="utf-8"
    )
    recovery = (ROOT / "buildlimits/group_recovery.py").read_text(encoding="utf-8")
    assert placement.index("supervisor.groups.release(running)") < placement.index(
        "supervisor.outcomes.launched(entry, group_id)"
    ), "launch observation follows scope release"
    assert recovery.index("supervisor._apply(supervisor.admission.group_emptied") < (
        recovery.index("supervisor.outcomes.emptied(ending)")
    ), "ending observation follows admission's empty effect"


def test_admission_trace_is_identical_with_the_full_shell_on_and_off(
    tmp_path: Path,
) -> None:
    """Recording preserves calls, effects and token-return timing."""
    enabled = _differential_run(tmp_path / "enabled", recording=True)
    disabled = _differential_run(tmp_path / "disabled", recording=False)

    assert enabled.input_trace == disabled.input_trace, "inputs are unchanged"
    assert enabled.dispatched_effects == disabled.dispatched_effects, (
        "admission dispatches the same effects"
    )
    assert enabled.token_return_ticks == disabled.token_return_ticks, (
        "the token returns on the same loop tick"
    )
    assert enabled.group_states == ["server-drain"], (
        "the non-vacuous trace reaches SERVER_DRAIN"
    )
    assert any(name == "cache_outcome" for name, _fields in enabled.metrics), (
        "the active shell completes a reading for its cached launch"
    )
    assert any(record for record in enabled.records), "the completed window is stored"
    assert enabled.pool_tokens == disabled.pool_tokens == [b"+"], (
        "the fake pool regains exactly the same token"
    )


def test_differential_oracle_detects_a_token_wait_during_a_query(
    tmp_path: Path,
) -> None:
    """The paired trace changes if a shell waits for its query before admission."""
    baseline = _differential_run(tmp_path / "baseline", recording=True)
    mutated = _differential_run(tmp_path / "mutated", recording=True, delay_token=True)

    assert baseline.input_trace != mutated.input_trace, (
        "a query-dependent token delay changes the observed admission inputs"
    )
    assert baseline.token_return_ticks != mutated.token_return_ticks, (
        "the differential oracle includes the shifted return tick"
    )
