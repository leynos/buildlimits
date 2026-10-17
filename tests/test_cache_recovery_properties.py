"""The recovery transaction's safety contract, over arbitrary interleavings.

The scripted tests fix one scenario each, which is what makes them readable.
What they cannot show is that the contract holds for the orderings an operator
and the admission path actually produce between them: a compiler group admitted
between the trigger and the stop, an accounting proof that arrives late, a
restart refused while the barrier is already gone. That is a property of every
reachable interleaving rather than of any one example, so it is stated once and
searched.

Each part of the contract is written so that it would fail if the production
guard it names were removed. What is searched is the transaction's *control*
safety — which verbs it is allowed to issue, in which order, and what it
reports once it ends. Token conservation is deliberately not claimed here: the
model in :mod:`cache_recovery_model` derives ``held`` from the gauge axes
instead of observing a token move, so a search over it would assume the very
relation it would appear to test. That obligation is discharged by
:mod:`test_cache_recovery` against the real pool double.

Two are invariants over the run, checked after every rule:

- no start is issued while the barrier is still in place, which is the ordering
  that keeps the unit's own ``ConditionPathExists=!`` from skipping a start the
  supervisor believes it issued; and
- an idle transaction has nothing pending — no failure counted and no retry
  due — so the operator's answer does not depend on when it happens to be
  asked.

Two are properties of the moment a verb is issued or an ending observed, which
is why they are asserted as each step is served rather than as invariants: a
group admitted after a lawful stop must not retroactively count as
interrupted, and the phase a transaction reports after it ends must be read
while the ending is the one under observation.

- no stop is issued while a compiler group is moving, which is the guard that
  stops admission being able to interrupt a compile;
- no stop is issued while the manager transport is faulted, which is the guard
  that keeps an unreadable service from being stopped on a guess; and
- an ending keeps its fault exactly when its last restart was refused: a
  restart that was accepted ends clean, and one that gave up at the failure
  limit keeps the reason the operator must read. Requiring every vanished
  transaction to be fault-free would reject the second, and treating a stale
  fault as current would misreport the first.

The model in :mod:`cache_recovery_model` tracks only what those checks
quantify over. Rules that withdraw a precondition deliberately leave the
transaction running rather than rewinding it, because the ordering contract is
about what happens between a proof arriving and the next action being
attempted — rewinding would erase exactly the window under test.

The search itself, and the event ledger it fills, live in
:mod:`cache_recovery_state_machine`.
"""

import tempfile
from pathlib import Path

from cache_recovery_helpers import CacheState
from cache_recovery_state_machine import Events, RecoveryMachine
from hypothesis import settings
from hypothesis.stateful import run_state_machine_as_test

from buildlimits.recovery_guidance import GUIDANCE

# Every control event the contract quantifies over. The guards are properties
# of an action, so a search that never performs one would satisfy them all
# without testing any: a run that never opened a transaction, never stopped the
# cache and never restarted it is an empty prefix of the contract rather than a
# proof of it. Each is asserted non-zero below against the ledger the run
# filled. It is a tuple rather than a set so the failure message names the
# missing events in the order the contract introduces them.
EVENTS = ("opened", "stopped", "started")


def test_the_recovery_safety_contract_holds_for_any_interleaving(
    tmp_path: Path,
) -> None:
    """Search the reachable interleavings, and check the search reached them.

    Hypothesis reports the shortest failing rule sequence, which names the
    interleaving that broke a rule rather than just the assertion that noticed.

    Non-vacuity is measured rather than assumed, and measured on *this* run.
    Every assertion in the contract is a property of an action — a stop, a
    start, an ending — so a search whose rules happened never to perform one
    would pass while testing nothing. Reading the ledger the run itself filled
    is what distinguishes this from the coverage note it replaces: a recorded
    count can be wrong, and asserting it is what would notice. A separate,
    cheaper search asserting reachability would establish it about a different
    run, which is exactly the claim that was missing.
    """
    seen: list[Events] = []
    run_state_machine_as_test(
        lambda: _recording_machine(tmp_path, seen),
        settings=settings(max_examples=200, stateful_step_count=60, deadline=None),
    )

    assert seen, "the search ran at least one example to read a ledger from"
    totals = {event: sum(getattr(ledger, event) for ledger in seen) for event in EVENTS}
    assert all(count > 0 for count in totals.values()), (
        f"the search never reached every event the guards judge: {totals}"
    )


def _recording_machine(tmp_path: Path, seen: list[Events]) -> RecoveryMachine:
    """Build one example's machine and keep the ledger it fills.

    Parameters
    ----------
    tmp_path : Path
        The directory this example's transaction runs in.
    seen : list[Events]
        The ledgers already collected; this example's is appended to it.

    Returns
    -------
    RecoveryMachine
        The machine the search drives, whose ``events`` the caller reads once
        the example has run.
    """
    machine = RecoveryMachine(Path(tempfile.mkdtemp(dir=tmp_path)))
    seen.append(machine.events)
    return machine


def test_the_invariants_name_the_guards_they_protect() -> None:
    """The contract reads a catalogue that could genuinely be missing an entry.

    The search can only show that the invariants hold; this pins the
    assumptions they rest on, so a guard or an operator action removed from the
    production module is a change this module would notice. It is a check on
    the contract's coverage, not on the search.
    """
    assert GUIDANCE.keys() == {"quiescing", "stopping", "restarting"}, (
        "the operator guidance covers exactly the phases that can fail"
    )
    assert "accounting" not in GUIDANCE, (
        "accounting waits for proof rather than failing, so it needs no action"
    )
    assert sorted(CacheState().verbs) == [], "a fresh double has issued no verbs"


# Non-vacuity, discharged by mutation and recorded here because the evidence is
# a manual experiment rather than something the suite can assert. Each mutation
# was applied to the production module, the search was re-run, and the mutation
# was then reverted:
#
#   * removing the ``self.barrier.clear()`` call from ``_try_start`` (so a
#     restart races the unit's own ``ConditionPathExists=!``) is rejected by
#     ``no_start_is_queued_behind_a_live_barrier``;
#   * removing the ``_blocked`` guard from ``_try_stop`` outright is rejected by
#     the two stop assertions in ``tick``;
#   * clearing ``_last_fault`` in ``_finish`` (so an exhausted ending loses the
#     fault it gave up over) is rejected by the ``restart_runs_out`` rule
#     reaching the ending with the refusal still scripted;
#   * removing the ``_last_fault = None`` line from ``_try_start``'s accepted
#     path (so an accepted restart keeps an earlier attempt's fault) is
#     rejected by the same check's other branch.
#
# The ending-fault check's reachability is a recorded observation as well: an
# instrumented run of the rule order alone reached the restart phase 36 times
# and scripted 17 refusals without ever observing an exhausted ending, because
# that ending needs the refusal held across ``RECOVERY_FAILURE_LIMIT``
# consecutive due steps. The ``restart_runs_out`` rule is what makes the
# faulted half of the check reachable instead of dead.
#
# One mutation that the search does *not* reject is worth naming, because the
# reason is a property of the design rather than a gap: deleting either
# individual clause from ``_blocked`` still leaves the stop blocked by the
# remaining clauses — ``held == server_drain`` already implies no group is
# moving, and ``request_stop`` re-reads the manager fault itself before issuing
# the verb. The guards are therefore defence in depth, and the search's
# sensitivity is asserted at the choke point (``_try_stop``) rather than at each
# redundant clause. A future change that makes those clauses load-bearing would
# be caught by the scripted tests in :mod:`test_cache_recovery_machine`, which
# pin each refusal's own reason text.
