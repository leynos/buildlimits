"""The stateful search over the recovery transaction's reachable orderings.

:class:`RecoveryMachine` drives the real
:class:`~buildlimits.cache_recovery.CacheRecovery` over the scripted cache
from :mod:`cache_recovery_machines` and the model from
:mod:`cache_recovery_model`. It lives beside its test rather than inside it for
the same reason the machine under test lives in its own module: the search
carries the contract, the ledger and the recorded refusals, and a reader
following one of those should not have to read past the other two.

The machine also records the *events* each rule produced, which is what makes a
passing search evidence about the contract rather than about an empty prefix of
it. A search whose rules never open a transaction, never stop the cache and
never restart it would satisfy every guard in the contract vacuously: each
guard is a property of an action, and an action never taken has no property.
:class:`~cache_recovery_events.Events` is the ledger those counts land in; the
test module asserts they are non-zero across the generated run.
"""

import typing as typ

import pytest
from cache_recovery_events import CalmClock, Events, Moment
from cache_recovery_machines import scripted_machine
from cache_recovery_model import MAX_GROUPS, Model
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from buildlimits import cache_recovery

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cache_recovery_helpers import CacheState

# How far the hand-driven clock moves per step. It must exceed the longest
# backoff (``RECOVERY_MAX_RETRY_SECONDS``), or a transaction that faults once
# would never be due again and the search would stop reaching its guards — the
# exact vacuity :func:`test_the_search_reaches_every_event_it_judges` guards
# against. The retry schedule itself is exercised by the scripted tests; here
# the point is to reach every phase and every refusal, not to time the retries.
STEP_SECONDS = 200.0

# The roots grace as a literal, so a shorter production deadline is a
# disagreement here rather than a quiet update.
ROOTS_GRACE_SECONDS = 600.0

# A settled retry is reported as a rounded duration, so "nothing pending" is
# read to within a float's rounding rather than by exact equality. Built once
# at module scope: constructing it inside the invariant would make the import
# look like it existed only for a type annotation.
_NO_RETRY = pytest.approx(0.0)


class RecoveryMachine(RuleBasedStateMachine):
    """Drive the recovery transaction and check its safety contract.

    Attributes
    ----------
    events : Events
        The control events this example produced, read by the test module to
        establish that the search was not vacuous.
    """

    def __init__(self, tmp_path: Path) -> None:
        """Build the scripted cache and the transaction over it.

        Parameters
        ----------
        tmp_path : Path
            A directory used by this example alone. A transaction leaves its
            barrier on disk, so sharing one between examples would open a run
            with a barrier no transaction of its own had written.
        """
        super().__init__()
        self.model = Model()
        cached = scripted_machine(tmp_path, [self.model.counts])
        self.machine = cached.machine
        self.state: CacheState = cached.cache
        self.snapshot = cached.snapshot
        self.roots = cached.roots
        self.kind = cached.kind
        self.clock = 0.0
        self.events = Events()
        # Whether the open transaction came from the automatic trigger rather
        # than an operator request, which skips the zero-root proof by design.
        self._automatic = False
        self._calm = CalmClock()
        self._sync()

    def _phase(self) -> str:
        """Read the phase the transaction reports at the hand-driven clock.

        Returns
        -------
        str
            The phase the transaction reports, so a rule can branch on it
            without re-reading the document it took a moment from.
        """
        return str(self.machine.describe(self.clock)["phase"])

    def _moment(self) -> Moment:
        """Take the phase and verb counts a rule is about to change.

        Returns
        -------
        Moment
            The reading a rule compares against once its step has run.
        """
        return Moment(
            self._phase(),
            self.state.verbs.count("stop"),
            self.state.verbs.count("start"),
        )

    def _note(self, before: Moment) -> None:
        """Record the control events the step between the moments produced.

        Parameters
        ----------
        before : Moment
            The reading taken before the step.
        """
        if before.phase == "idle" and self._phase() != "idle":
            self.events.opened += 1
        self.events.stopped += self.state.verbs.count("stop") - before.stops
        self.events.started += self.state.verbs.count("start") - before.starts

    def _sync(self) -> None:
        """Publish the model's scripted readings to the next tick."""
        self.snapshot[0] = self.model.counts
        # A manager fault replaces the live-work fault rather than joining it:
        # the live watch reports one kind, and only the live-work kind carries
        # a root count for the zero-root proof to read.
        self.kind[0] = "manager" if self.model.manager_faulted else "live_work"
        self.roots[0] = self.model.roots
        self.state.active = self.model.active

    def _advance(self) -> None:
        """Move the clock past any backoff and ask for one due step."""
        self.clock += STEP_SECONDS
        self.machine.tick(self.clock)

    def _serve_the_restart_to_its_ending(self) -> None:
        """Advance due steps until the refused restart ends or the limit passes.

        An exhausted ending needs :data:`RECOVERY_FAILURE_LIMIT` consecutive
        refusals, which the rule order as good as never draws (an instrumented
        search never observed a refused ending), so the branch that reads the
        fault an ending keeps would be dead without this rule. Each step is
        still one due step at a time, so the fault is read at the ending.
        """
        for _ in range(cache_recovery.RECOVERY_FAILURE_LIMIT + 1):
            before = self._phase()
            if before != "restarting":
                return
            self._advance()
            self._check_the_ending(before)

    def _check_the_ending(self, before: str) -> None:
        """Judge the fault an ending keeps, if one was just observed.

        The restart step is the only one that ends a transaction: it either
        accepted the start or gave up at the failure limit. The fault the
        ending keeps must be exactly that outcome's, and the scripted
        manager's refusal is what tells the two apart from outside the
        machine. Checked at the moment the ending is observed rather than as
        an invariant, because a later transaction would overwrite the fault
        it reads.

        Parameters
        ----------
        before : str
            The phase the transaction reported before the step under
            observation.
        """
        if before != "restarting" or self._phase() != "idle":
            return
        kept = self.machine.describe(self.clock)["last_fault"] is not None
        if self.state.refuse == "start":
            assert kept, "a restart that gave up keeps the reason it gave up"
        else:
            assert not kept, "a restart that was accepted ends without a fault"

    @rule()
    def tick(self) -> None:
        """Advance the transaction by one due step and check its guards.

        The stop's two preconditions are asserted here rather than as
        invariants because they are properties of the moment the verb was
        issued, not of the run afterwards: a group admitted *after* a lawful
        stop must not retroactively count as having been interrupted. The
        model is read before the step, which is exactly the snapshot the
        transaction's own ``_blocked`` guard reads from ``counts``.
        """
        moved = self.model.moving
        faulted = self.model.manager_faulted
        unproven = self.model.roots != 0
        before = self._moment()
        is_calm = self._phase() != "idle" and not (moved or faulted)
        self._calm.observe(is_calm=is_calm, now=self.clock + STEP_SECONDS)

        self._advance()
        self._check_the_ending(before.phase)
        self._note(before)
        # A tick opens a transaction only through the automatic trigger, so an
        # opening under a moving group or a visible root is the stale path.
        if before.phase == "idle" and self._phase() != "idle":
            self._automatic = True
            self.events.opened_stale += bool(moved or unproven)

        if self.state.verbs.count("stop") > before.stops:
            assert moved == 0, "a stop was issued while a compiler group was moving"
            assert not faulted, "the cache was stopped while its state was unreadable"
            if self._automatic and unproven:
                # Only the deadline can authorize this, and only after the
                # roots have been visible with nothing cached moving.
                assert self._calm.since is not None, (
                    "an automatic stop was issued with roots visible and no calm"
                )
                assert self.clock - self._calm.since >= ROOTS_GRACE_SECONDS, (
                    "an automatic stop was issued with roots visible before the "
                    "grace had passed"
                )
                self.events.stopped_on_deadline += 1
        # The scripted manager settles the service on every control call, so
        # the model takes the cache's running state from the double rather
        # than assuming the job's effect.
        self.model.active = self.state.active

    @rule(running=st.integers(min_value=0, max_value=MAX_GROUPS))
    def group_moves(self, running: int) -> None:
        """Put compiler groups into the running state."""
        self.model.running = running
        self.model.draining = 0
        self._sync()

    @rule(count=st.integers(min_value=0, max_value=MAX_GROUPS))
    def groups_drain(self, count: int) -> None:
        """End leader processes, leaving the groups draining."""
        self.model.running = max(0, self.model.running - count)
        self.model.draining = min(MAX_GROUPS, self.model.draining + count)
        self._sync()

    @rule(count=st.integers(min_value=0, max_value=MAX_GROUPS))
    def server_accounts(self, count: int) -> None:
        """Let the drain probe account for outstanding empty groups.

        This only removes a token from ``server_drain``; the drain probe alone
        releases one, by proving the service stopped and empty.
        """
        released = min(count, self.model.server_drain)
        self.model.server_drain -= released
        self._sync()

    @rule(present=st.booleans())
    def a_token_waits_on_the_server(self, *, present: bool) -> None:
        """Grant or withhold one empty group's token for server work."""
        self.model.server_drain = 1 if present else 0
        self._sync()

    @rule(faulted=st.booleans())
    def manager_transport(self, *, faulted: bool) -> None:
        """Fault or restore the user-manager transport."""
        self.model.manager_faulted = faulted
        self._sync()

    @rule(present=st.booleans())
    def cache_reachable(self, *, present: bool) -> None:
        """Remove the managed service, as an admin stopping it by hand does."""
        self.model.active = present
        self._sync()

    @rule(refused=st.booleans())
    def restart_refused(self, *, refused: bool) -> None:
        """Script whether the manager accepts the restart verb.

        This is the knob the exhausted ending is produced through: a refusal
        held while the restart's attempts run out is what makes the ending
        faulted rather than clean.
        """
        self.state.refuse = "start" if refused else None

    @rule()
    def restart_runs_out(self) -> None:
        """Hold the refusal and serve the restart through to its ending.

        The ending-fault check covers both outcomes an ending can have, but a
        clean ending is the only one the rule order otherwise reaches: the
        faulted ending needs every restart attempt refused, and the search
        would have to draw that refusal and eight further ticks in one
        uninterleaved run. This rule takes the draw out of the search's
        hands, so the branch that reads an exhausted ending's fault is
        reached on purpose rather than left dead.
        """
        self.state.refuse = "start"
        before = self._moment()
        self._serve_the_restart_to_its_ending()
        self._note(before)

    @rule()
    def request(self) -> None:
        """Ask for a transaction, as the operator or the policy does."""
        before = self._moment()
        self.machine.request("manual", self.clock)
        if before.phase == "idle" and self._phase() != "idle":
            self._automatic = False
        self._note(before)

    @precondition(lambda self: self._phase() == "idle")
    @rule()
    def starve(self) -> None:
        """Offer the automatic trigger a starving snapshot with a clean proof.

        Offering the reading is all this rule does; the transaction it may
        authorize opens on the next :meth:`tick`, through the production
        trigger, so the count this rule contributes is the opening that tick
        produces rather than one the rule manufactured.
        """
        self.model.running = 0
        self.model.draining = 0
        self.model.manager_faulted = False
        self.model.roots = 0
        self.model.server_drain = max(1, self.model.server_drain)
        self.model.queued = max(1, self.model.queued)
        self._sync()

    @precondition(lambda self: self._phase() == "idle")
    @rule()
    def a_busy_drain_goes_stale(self) -> None:
        """Hold a retained drain under cached work until only its age can act.

        The staleness proof needs one retention unbroken for 300 seconds while
        the prompt proof is absent, and the other rules reset the drain far
        more often than two uninterrupted ticks occur. As with
        :meth:`restart_runs_out`, this rule takes the draw out of the search's
        hands so the stale opening is reached on purpose. Each tick still goes
        through :meth:`tick`, so the stop's guards judge every step it takes.
        """
        # Start from the starving reading, then defeat its prompt proof.
        self.starve()
        self.model.running, self.model.roots = 1, 2
        self._sync()
        for _ in range(2):
            self.tick()

    @precondition(lambda self: self._phase() == "idle")
    @rule()
    def a_hung_root_outlives_the_clients(self) -> None:
        """Hold a root visible after every cached group has ended.

        The deadline needs an automatic transaction, nothing cached moving and
        roots visible across several due stop attempts, a run the search is
        unlikely to draw; each tick still goes through :meth:`tick`.
        """
        self.a_busy_drain_goes_stale()
        self.model.running = 0
        self.model.draining = 0
        self.model.manager_faulted = False
        self._sync()
        for _ in range(8):
            self.tick()

    @invariant()
    def no_start_is_queued_behind_a_live_barrier(self) -> None:
        """Check that no start is queued behind a barrier still in place.

        Checked against the barrier's presence *at the moment of the call*,
        which the scripted manager records for exactly this reason: a barrier
        cleared later in the run must not excuse a start that was issued
        behind it.
        """
        if self.state.marker_at_start is None:
            return
        assert self.state.marker_at_start is False, (
            "a start was issued behind a barrier the unit's condition would skip"
        )

    @invariant()
    def an_idle_transaction_has_nothing_pending(self) -> None:
        """Check that an idle transaction is not waiting on a retry.

        This is the part of the ended-state contract that holds for every
        ending: whatever fault it kept, nothing is pending. A target with
        attempts in flight or a retry still due would be a transaction that
        reports itself idle while still working.

        The fault an ending keeps is checked separately, in ``tick``, because
        it is a property of the ending rather than of the idle state that
        follows: a fault kept by an exhausted restart is correct, and a later
        transaction would overwrite what this invariant could read.

        Every idle moment in the run is judged, not the first alone.
        """
        document = self.machine.describe(self.clock)
        if document["phase"] != "idle":
            return
        assert document["attempts"] == 0, "an idle transaction counted no failures"
        assert document["retry_in"] == _NO_RETRY, "an idle transaction awaits no retry"
