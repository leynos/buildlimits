"""Generated histories keep the recording shell from perturbing admission."""

import tempfile
import typing as typ
from pathlib import Path

from differential_trace_test_helpers import (
    CompletingStatistics,
    Trace,
    cached_entry,
)
from hypothesis import event, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
)
from outcome_shell_test_support import (
    OutcomeWorld,
    OutcomeWorldOptions,
    make_outcome_world,
)

from buildlimits.admission import Launch
from buildlimits.admission_state import GroupState
from buildlimits.effects import Exit
from buildlimits.supervisor_outcome_setup import GroupEnding

if typ.TYPE_CHECKING:
    from buildlimits.effects import Effect


_CLEAN_EXIT = Exit(code=0)
_EXIT_CHOICES = (_CLEAN_EXIT, Exit(code=1), Exit(signal=9))


class DifferentialMachine(RuleBasedStateMachine):
    """Replay one generated admission history through recording on and off.

    One world runs the full recording shell and the other the null shell. Every
    operation is applied to both, and the authority-side trace is compared
    after each step, so a recording path that changed a decision, an effect or
    a token's return tick fails even when no fixed scenario reaches it.

    Hypothesis draws a different subset of rules for each example, so a fixed
    seed first completes one cached request and its reading. Every example then
    compares a real recording trace rather than only an empty one.
    """

    def __init__(self) -> None:
        """Create both worlds under one temporary directory and clock."""
        super().__init__()
        self._directory = tempfile.TemporaryDirectory()
        root = Path(self._directory.name)
        self.clock = [0.0]
        self.enabled = Trace(
            self._make_world(root / "enabled", recording=True), self.clock
        )
        self.disabled = Trace(
            self._make_world(root / "disabled", recording=False), self.clock
        )
        self.now = 0.0
        self.requests = 0
        self.is_cached: dict[str, bool] = {}
        self.exits: dict[str, Exit] = {}
        self.is_seeded = False

    def _make_world(self, root: Path, *, recording: bool) -> OutcomeWorld:
        """Build one world whose shells read the machine's shared clock."""
        return make_outcome_world(
            root,
            OutcomeWorldOptions(
                recording=recording,
                statistics_port=CompletingStatistics(),
                clock=lambda: self.clock[0],
            ),
        )

    def teardown(self) -> None:
        """Remove the run's temporary directory."""
        self._directory.cleanup()
        super().teardown()

    @initialize()
    def seed_a_completed_reading(self) -> None:
        """Warm a draining and a completed cached group before the rules run.

        A signal exit leaves its token in server drain; a clean exit after it
        returns a token and completes the window. Every example therefore
        compares a real recording rather than an empty one.
        """
        for now in (0.0, 0.1):
            self._advance_to(now)
        self.requests += 2
        self._apply("submit", (1,), {"cacheable": True})
        self._apply("submit", (2,), {"cacheable": True})
        draining = self._deliver(b"+")
        completed = self._deliver(b"t1")
        assert draining is not None, "the first queued request holds its token"
        assert completed is not None, "the second queued request holds its token"
        self._end(draining, Exit(signal=9))
        self._empty(draining)
        self._end(completed, _CLEAN_EXIT)
        self._empty(completed)
        self._apply("server_activity", (0,))
        for now in (1.1, 1.2, 2.3, 2.4):
            self._advance_to(now)
        self.is_seeded = True

    @rule(step=st.sampled_from([0.05, 0.2, 1.0, 2.0]))
    def advance(self, step: float) -> None:
        """Advance the shared clock and both outcome shells."""
        self._advance_to(self.now + step)

    @rule(cacheable=st.booleans())
    def submit(self, cacheable: bool) -> None:
        """Queue one new request in both admissions."""
        self.requests += 1
        self._apply("submit", (self.requests,), {"cacheable": cacheable})

    @rule(token=st.sampled_from([b"+", b"t1", b"t2"]))
    def token_arrived(self, token: bytes) -> None:
        """Deliver one pool token to both admissions."""
        self._deliver(token)

    @rule(exit_=st.sampled_from(_EXIT_CHOICES))
    def leader_exited(self, exit_: Exit) -> None:
        """End the leader of the oldest live group in both admissions."""
        group_id = self._oldest_live_group()
        if group_id is not None:
            self._end(group_id, exit_)

    @rule()
    def group_emptied(self) -> None:
        """Empty the oldest live group in both admissions."""
        group_id = self._oldest_live_group()
        if group_id is not None:
            self._empty(group_id)

    @rule(active_compiles=st.integers(min_value=0, max_value=3))
    def server_activity(self, active_compiles: int) -> None:
        """Poll the server's load in both admissions."""
        self._apply("server_activity", (active_compiles,))

    @invariant()
    def recording_preserves_the_authority_trace(self) -> None:
        """Compare both traces and keep the generated cases non-vacuous."""
        self._assert_identical()
        assert self.disabled.world.metrics.events == [], "the null shell is inert"
        if not self.is_seeded:
            return
        observations = self.enabled.observations()
        assert observations.records, "the seed completed and stored one reading"
        if self.is_cached:
            event("launch")
        if "server-drain" in observations.group_states:
            event("server-drain")
        if any(name == "cache_outcome" for name, _fields in observations.metrics):
            event("cache-outcome")

    def _advance_to(self, now: float) -> None:
        """Move the shared clock to ``now`` and tick both shells."""
        self.now = now
        for trace in (self.enabled, self.disabled):
            trace.tick(now)

    def _deliver(self, token: bytes) -> str | None:
        """Deliver one token, mirroring any launch into both shells.

        Returns
        -------
        str | None
            The launched group, or ``None`` when nothing was waiting.
        """
        effects = self._apply("token_arrived", (token,))
        launch = next((item for item in effects if isinstance(item, Launch)), None)
        if launch is None:
            return None
        self.is_cached[launch.group_id] = launch.cached
        for trace in (self.enabled, self.disabled):
            trace.world.shell.launched(cached_entry(), launch.group_id)
        return launch.group_id

    def _end(self, group_id: str, exit_: Exit) -> None:
        """End one group's leader in both admissions."""
        self.exits[group_id] = exit_
        self._apply("leader_exited", (group_id, exit_))

    def _empty(self, group_id: str) -> None:
        """Empty one group and mirror its ending into both shells."""
        self._apply("group_emptied", (group_id,))
        exit_ = self.exits.get(group_id, _CLEAN_EXIT)
        ending = GroupEnding(
            group_id, self.is_cached.get(group_id, False), exit_ == _CLEAN_EXIT
        )
        for trace in (self.enabled, self.disabled):
            groups = trace.world.admission.groups
            state = groups[group_id].state.value if group_id in groups else "removed"
            trace.group_states.append(state)
            trace.world.shell.emptied(ending)

    def _apply(
        self,
        name: str,
        args: tuple[object, ...],
        kwargs: dict[str, object] | None = None,
    ) -> list[Effect]:
        """Run one operation on both admissions and compare their effects.

        Returns
        -------
        list[Effect]
            The effects the recording world dispatched, which the null world
            must have dispatched identically.
        """
        options: dict[str, object] = {} if kwargs is None else kwargs
        recorded = (*args, *sorted(options.items()))
        produced: list[Effect] = []
        for trace in (self.enabled, self.disabled):
            produced = getattr(trace.world.admission, name)(*args, **options)
            trace.observe(name, recorded, produced)
        assert self.enabled.dispatched_effects == self.disabled.dispatched_effects, (
            f"{name} dispatches identical effects with recording on and off"
        )
        return produced

    def _oldest_live_group(self) -> str | None:
        """Return the oldest group that has not already entered server drain.

        Returns
        -------
        str | None
            A group that can still end, or ``None`` when every group drains.
        """
        for group_id, group in self.enabled.world.admission.groups.items():
            if group.state is not GroupState.SERVER_DRAIN:
                return group_id
        return None

    def _assert_identical(self) -> None:
        """Fail if the worlds observed different authority behaviour."""
        enabled = self.enabled.observations()
        disabled = self.disabled.observations()
        assert enabled.input_trace == disabled.input_trace, "inputs are unchanged"
        assert enabled.dispatched_effects == disabled.dispatched_effects, (
            "admission dispatches the same effects"
        )
        assert enabled.token_return_ticks == disabled.token_return_ticks, (
            "tokens return on the same tick"
        )
        assert enabled.group_states == disabled.group_states, "groups end identically"
        assert enabled.pool_tokens == disabled.pool_tokens, (
            "the pool regains its tokens"
        )


TestDifferentialMachine = DifferentialMachine.TestCase
TestDifferentialMachine.settings = settings(
    max_examples=30, stateful_step_count=15, deadline=None
)
