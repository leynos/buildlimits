"""Admission traces stay identical with the recording shell on or off."""

import ast
import typing as typ
from pathlib import Path

from differential_trace_test_helpers import (
    CompletingStatistics,
    DifferentialObservations,
    Trace,
    cached_entry,
)
from import_graph_test_helpers import package_closure
from outcome_shell_test_support import OutcomeWorldOptions, make_outcome_world

from buildlimits.admission import Launch
from buildlimits.effects import Exit, ReturnToken
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
_DECISION_MODULES = (
    "admission",
    "admission_state",
    "effects",
    "server_watch",
    "cache_recovery",
    "recovery_starvation",
    "recovery_roots",
    "recovery_outlook",
)

if typ.TYPE_CHECKING:
    from buildlimits.supervisor_outcomes import OutcomeShell


def _delay_query_for_token(
    shell: OutcomeShell, *, is_enabled: bool, now: float, clock: list[float]
) -> None:
    """Delay one pending query so the comparison changes token timing."""
    if is_enabled and isinstance(shell._recorder.state, Querying):
        clock[0] = now
        shell.on_tick(now)


def _differential_run(
    tmp_path: Path, *, recording: bool, delay_token: bool = False
) -> DifferentialObservations:
    """Drive one cache-group trace and return its authority-side observations.

    Returns
    -------
    DifferentialObservations
        Admission traces, token timings and optional recording outputs.
    """
    clock = [0.0]
    world = make_outcome_world(
        tmp_path,
        OutcomeWorldOptions(
            recording=recording,
            statistics_port=CompletingStatistics(),
            clock=lambda: clock[0],
        ),
    )
    trace = Trace(world, clock)
    _warm_previous_group(trace, recording=recording)
    trace.observe("submit", (1,), world.admission.submit(1))
    _delay_query_for_token(
        typ.cast("OutcomeShell", world.shell),
        is_enabled=delay_token and recording,
        now=1.2,
        clock=clock,
    )
    _drain_one_group(trace)
    trace.tick(2.3)
    trace.tick(2.4)
    return trace.observations()


def _warm_previous_group(trace: Trace, *, recording: bool) -> None:
    """Run one completed cached request so a statistics read becomes due."""
    trace.tick(0.0)
    trace.tick(0.1)
    trace.world.shell.launched(cached_entry(), "previous")
    trace.world.shell.emptied(GroupEnding("previous", True, True))
    trace.tick(1.1)
    if recording:
        state = typ.cast("OutcomeShell", trace.world.shell)._recorder.state
        assert isinstance(state, Querying), "the recorder starts a due statistics query"


def _drain_one_group(trace: Trace) -> None:
    """Launch one cached group, drain it, and record its held token state."""
    admission = trace.world.admission
    token = trace.pool_tokens.pop(0)
    effects = trace.observe("token_arrived", (token,), admission.token_arrived(token))
    launch = next(effect for effect in effects if isinstance(effect, Launch))
    trace.world.shell.launched(cached_entry(), launch.group_id)
    trace.observe(
        "leader_exited",
        (launch.group_id, Exit(signal=9)),
        admission.leader_exited(launch.group_id, Exit(signal=9)),
    )
    empty_effects = trace.observe(
        "group_emptied",
        (launch.group_id,),
        admission.group_emptied(launch.group_id),
    )
    trace.group_states.append(admission.groups[launch.group_id].state.value)
    trace.world.shell.emptied(GroupEnding(launch.group_id, True, False))
    assert not any(isinstance(effect, ReturnToken) for effect in empty_effects), (
        "admission keeps the token while its group drains"
    )
    trace.observe("server_activity", (0,), admission.server_activity(0))
    if trace.clock[0] < 1.2:
        trace.tick(1.2)


def _import_parts(tree: ast.Module) -> set[str]:
    """Return every dotted part of every module name the imports mention.

    Both spellings reach recording code: ``from buildlimits.outcome_x import
    Y`` hides the module in ``ImportFrom.module``, and ``import
    buildlimits.outcome_x`` hides it inside a dotted ``alias.name``. Checking
    each part catches either form without matching a symbol name by accident.

    Examples
    --------
    >>> sorted(_import_parts(ast.parse("from buildlimits.outcome_x import Y")))
    ['Y', 'buildlimits', 'outcome_x']
    """
    names: list[str] = []
    for node in ast.walk(tree):
        match node:
            case ast.Import(names=aliases):
                names.extend(alias.name for alias in aliases)
            case ast.ImportFrom(module=module, names=aliases):
                names.extend([module] if module else [])
                names.extend(alias.name for alias in aliases)
    return {part for name in names for part in name.split(".")}


def test_decision_modules_do_not_import_outcome_recording() -> None:
    """Admission decisions remain independent of optional recording code."""
    package = ROOT / "buildlimits"
    for filename in _ADMISSION_MODULES:
        tree = ast.parse((package / filename).read_text(encoding="utf-8"))
        imported = _import_parts(tree)
        assert not any(part.startswith("outcome_") for part in imported), filename


def test_decision_module_closure_excludes_outcome_recording() -> None:
    """Nothing the decision roots reach may be recording code (C-OBS)."""
    closure = package_closure(ROOT / "buildlimits", _DECISION_MODULES)

    assert set(_DECISION_MODULES) <= closure, "the closure reaches every root"
    leaked = sorted(name for name in closure if name.startswith("outcome_"))
    assert not leaked, f"decision modules transitively reach recording code: {leaked}"


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
