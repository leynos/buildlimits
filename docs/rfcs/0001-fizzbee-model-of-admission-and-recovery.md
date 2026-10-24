# RFC 0001: FizzBee model of compile admission and cache recovery

## Preamble

- **RFC number:** 0001
- **Status:** Accepted
- **Created:** 2026-10-18
- **Accepted:** 2026-10-18; see [Acceptance](#acceptance)
- **Baseline:** `main` at `7445bf0` (PR #12), which carries the fixes for
  incidents 1, 2 and 4 (PR #9), for incident 3 (PR #10), and for the stale-path
  automatic stop that PR #10 left waiting for zero visible roots (PR #12)[^1].
- **Scope:** verification tooling for maintainers. Nothing in the package,
  its behaviour or its interfaces changes.

## Summary

Four defects in compile admission's cache recovery reached the shared
development host before any test found them. Each concerned behaviour _over
time_ when a busy queue, cancelling builds and the managed cache interact: a
recovery that could never stop the cache, a rest that left retained tokens idle
without explanation, a trigger whose evidence never coincided under load, and a
fault that vanished from status. The existing Hypothesis state machines and
CrossHair contracts check safety over short, bounded interleavings of hand-kept
models. None checks progress, none models a busy queue, and none composes
admission with recovery.

This RFC proposes a FizzBee[^2] model of the token pool, admission, the managed
cache, the drain probe and the four-phase recovery transaction, with the
environment as adversarial and fair actors. It states safety and liveness
properties under explicit fairness assumptions, argues that each incident would
have been a counterexample, and sets out how the model is kept honest against
the code. Adoption is gated: a spike must first reproduce a historical incident
as a counterexample on its pre-fix behaviour, and pass on the fixed behaviour,
before the full model, conformance checking and Continuous Integration (CI) are
built.

## Problem

### Production incidents

On 2026-10-18 four defects were observed on the shared host, and each was
confirmed against the code. Table 1 summarizes them.

| Incident         | Symptom                                     | Cause                                        | Resolution                                                 |
| ---------------- | ------------------------------------------- | -------------------------------------------- | ---------------------------------------------------------- |
| 1. Livelock      | Retained tokens held while the stop retried | Stop guard counted uncached groups as moving | PR #9, `Counts.cached_moving`                              |
| 2. Idle cooldown | New drains idle for ~15 min, not in status  | 900 s cooldown after clean endings too       | PR #9, 60 s settle and `cooling_for`                       |
| 3. Capacity loss | One slot lost for 30+ min, then all slots   | Trigger needed two readings to coincide      | PRs #10 and #12, stale proof, early open and stop deadline |
| 4. Hidden fault  | Refused restart's fault absent from status  | Idle transaction dropped its last fault      | PR #9                                                      |

_Table 1: Recovery incidents observed on 2026-10-18._

In incident 1 the transaction denies cache composition while it runs, so every
group it admits is uncached and cannot be interrupted by the stop. The stop
guard nevertheless counted those groups as moving work, so a busy queue
refilled each freed slot before every retried stop, and the retained tokens
stayed held. Incident 3 has two halves. The automatic trigger required a
live-work fault with zero visible compiler roots _and_ no cached group moving
in the same reading; under a busy queue the two never coincided, so one
retained server-drain token cost a slot for more than 30 minutes, until every
slot was retained. Meanwhile, `build-limits-recover` refused outright while any
cached compile ran, so the operator could not intervene either. Incidents 2 and
4 were silent: tokens were retained correctly, but status did not say why.

PR #10 fixed both halves of incident 3. The trigger now also accepts a retained
drain held without a break for `RECOVERY_STALE_DRAIN_SECONDS = 300`
(`recovery_starvation.proof` names it `stale`, the other proof `detached`)[^3].
`recovery_starvation.refusal` refuses to open a transaction only when the user
manager transport is faulted, so `build-limits-recover` and the automatic
trigger open while cached work runs. The stop itself still waits for that work
through `recovery_starvation.blocked`[^4]. At `37d08e5` a stale-opened
transaction did not stop the cache on age alone: `CacheRecovery._try_stop`
re-armed an automatic transaction until the drain probe again proves zero
visible roots. Visible roots that never clear therefore held the stop off,
which left the retained tokens in place. PR #12 closed that gap: once no cached
group is moving, an automatic stop proceeds after roots have stayed visible for
`RECOVERY_ROOTS_GRACE_SECONDS = 600`, because a root visible under the held
barrier serves no admitted client. A manual request is exempt from the proof,
and an unreadable root count neither starts nor completes the clock
(`recovery_roots.stop_verdict`)[^5].

### Why the existing verification did not find them

The suite verifies more than most (developer's guide §11.3). The gaps are
structural, not a lack of effort:

- The recovery search treats every modelled group as cached:
  `tests/cache_recovery_model.py` sets `cached_moving` to the sum of running
  and draining groups, so the variable behind incidents 1 and 3 is constant.
- Admission and recovery are searched separately. `tests/test_admission.py`
  has no recovery, hold or trigger; `tests/cache_recovery_state_machine.py`
  drives `CacheRecovery` over injected gauges with no pool, queue dynamics or
  token arrivals. Their coupling, through the cache hold, `cache_allowed` and
  `cached_moving`, is where incidents 1 and 3 live.
- Time moves in fixed 200-second steps (`STEP_SECONDS`), so the relative
  order of the settle, cooldown, staleness and retry timers is never explored.
- Every property checked is a safety property of a finite prefix. A livelock
  is an infinite behaviour with no finite counterexample, and no fairness
  assumption is stated that would make "eventually" meaningful.
- The searches are random and bounded: 300 and 200 examples of 60 steps. The
  starvation truth table in `tests/test_recovery_starvation.py` is exhaustive,
  but over one instant's readings, so it cannot express that two readings never
  coincide over time.
- CrossHair covers only resource-limit planning
  (`tests/crosshair_contracts.py`).

These tools answer the questions they ask well. They cannot ask whether
recovery makes progress against a busy queue.

## Current state

The supervisor is a functional core with an imperative shell: `Admission`
returns effects, and one `selectors` loop in `Supervisor` carries them out,
with only the pool's blocking reads on a second thread, reported through a pipe
(developer's guide §3 and §6). Each tick reaps leaders and notices emptied
scopes, then polls the server once a second: the drain probe,
`ServerWatch.drained_tokens`, runs before `CacheRecovery.tick`, which advances
at most one phase step (`supervisor_polling._poll_server`). Only
`ManagedSccache.accounted_drain`, on a verified stopped-and-empty service, lets
`Admission.server_activity` release a `SERVER_DRAIN` token. A running
transaction denies composition through `ServerWatch.hold_cache` and writes the
restart barrier before it stops the cache. Every supervisor decision is
therefore a pure transition over small state, executed atomically between
observations of the outside world, which suits model checking.

## Goals and non-goals

- Goals:
  - Check safety and liveness of admission and recovery together, under
    explicit fairness, against an adversarial environment.
  - Reproduce incidents 1 and 3 as counterexamples on their pre-fix
    behaviour, and keep those reproductions as permanent non-vacuity checks.
  - Keep the model in step with the code through a stated correspondence, a
    mechanical drift check and trace replay into the existing harness.
  - Turn every genuine counterexample into a regression test in `tests/`.
- Non-goals:
  - Proving the Python implementation correct. The model checks the design;
    conformance checking narrows the gap to the code but does not close it.
  - Modelling descriptors, framing, the wire protocol, launch placement,
    rendering or metric transport.
  - Replacing the existing tests, or changing any runtime behaviour,
    threshold or interface.

## Proposed design

### 1. Model scope

The model has two parts: the supervisor, whose actions transcribe code, and an
environment, whose actions are everything the supervisor only observes.
Supervisor state is kept as identifier-free counts by group class, as `Counts`
is, which keeps the state space small and matches the metrics:

- the pool's free tokens, and the tokens other users' supervisors hold;
- the queue as counts of cacheable and uncached requests, `token_requested`,
  and one distinguished _tracked request_ for per-request liveness, kept with
  the number of requests ahead of it because the queue is served in order[^6];
- groups by lifecycle (`RUNNING`, `DRAINING`, `SERVER_DRAIN`), composition
  and ending, so that `Group.may_orphan_server_work` is computable;
- the last reachability reading, and whether a recovery hold is in force;
- the drain probe's fault kind, whether it proved zero visible roots, found
  some visible, or could not tell, and the retained-drain age, which
  `RetainedDrainAge` measures on idle ticks and retires at the accounting
  observation, with the staleness limit as a timer class[^3];
- the transaction's phase, attempts (abstracted to a small limit), trigger,
  last fault and rest timer; the barrier; an operator request; health.

The environment comprises a _busy queue_ that may submit cacheable or uncached
requests whenever fewer than a bound wait, including straight after a token
returns; _builds_ whose leaders exit cleanly or by signal, and requesters who
cancel, a bounded number of times; the _managed cache_, live or stopped, with
its control group and the detached work that abnormal cached endings leave; the
_user manager_, which accepts or refuses jobs, completes them later, and may
fault and restore its transport; _timers_ for settle, cooldown, staleness,
visible-roots grace and backoff; and an _operator_.

Time is abstract. A timer is armed with a duration class and expires through an
environment action, so the checker explores every order in which timers expire
relative to other events. Where a property depends on relative durations, such
as settle being shorter than cooldown, timers count small integer units
advanced by a tick action, and the property names units.

### 2. Fairness assumptions

Liveness is checked only under the assumptions in Table 2, each stated in the
model beside the action it constrains. FizzBee documents weak (`fair` or
`fair<weak>`) and strong (`fair<strong>`) fairness on actions[^2].

| ID  | Assumption                                  | Basis                                                                  |
| --- | ------------------------------------------- | ---------------------------------------------------------------------- |
| F1  | Supervisor poll steps are weakly fair       | Weak, not strong: the 1 s poll is not synchronized with group endings  |
| F2  | Leaders exit and draining scopes empty      | Termination escalates to `SIGKILL` after `cancel_grace_seconds`        |
| F3  | Detached server work finishes or is stopped | A claim about sccache, not buildlimits                                 |
| F3a | Unknown root readings are finite            | The grace neither starts nor completes while the root count is unknown |
| F4  | Accepted jobs complete; faults are finite   | A claim about the systemd user manager                                 |
| F5  | Requested tokens arrive and are returned    | A claim about guildmaster and other users                              |
| F6  | Armed timers expire, in no set order        | Time is abstract; the checker orders expiries                          |
| F7  | The environment is otherwise free           | Submissions possible, never forced; cancellations bounded              |

_Table 2: Fairness assumptions under which liveness is checked._

F1 is weak because the poll may never land in a transient window, and incident
1 lived in such windows. F7 makes the busy queue an adversary that may keep the
queue non-empty for ever, while bounded cancellations keep `eventually always`
properties meaningful. F3 to F5 are claims about systemd, the kernel and
sccache rather than buildlimits; a property that needs one is only as strong as
that claim, and every counterexample report names the assumptions in force.

F3a is new since this document was accepted, and is stated beside the drain
probe it qualifies. `recovery_roots.roots_verdict` returns `waiting` while a
root is visible and the grace has not run, and `VisibleRootsGrace.observe`
restarts the clock whenever a reading does not establish that roots are
visible, so an unreadable root count at every attempt would keep the grace from
ever expiring. The model assumes that reading is eventually available; the
configuration `head-unknown-roots`, which drops F3a, is expected to fail L1,
and that is H4's "a grace that cannot expire" clause made checkable.

### 3. Properties

Safety properties hold in every reachable state or transition; liveness
properties hold under F1 to F7, and those that depend on the visible-roots
grace also under F3a. Table 3 lists them.

| ID  | Property                   | Statement                                                                                                                                                               |
| --- | -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| S1  | Stop safety                | A stop is issued only with no cached group running or draining and a readable manager                                                                                   |
| S2  | Token conservation         | Tokens sum to capacity; held tokens partition into running, draining and server-drain groups; waiting requests hold none; a token returns once, after its scope empties |
| S3  | Accounted release          | A `SERVER_DRAIN` token is released only on a reading of a stopped, empty service                                                                                        |
| S4  | Barrier ordering           | A start is issued only with the barrier absent; the barrier stands from stop to restart                                                                                 |
| S5  | Composition                | No group is composed with the cache under a hold or after a negative reachability reading                                                                               |
| S6  | Server work coverage       | Live server work never exceeds the tokens held by cached groups (ADR 011[^7])                                                                                           |
| S7  | Unhealthy quiescence       | Nothing launches while the supervisor is unhealthy                                                                                                                      |
| S8  | Explainable retention      | A retained token with a waiting request and no transaction always has a reason in status                                                                                |
| S9  | Fault persistence          | A refused restart's fault stays in status until a later transaction ends cleanly                                                                                        |
| L1  | Retained drains accounted  | Under the `starved` policy, eventually always no token is in `SERVER_DRAIN`                                                                                             |
| L2  | Queue progress             | The tracked request, unless cancelled or refused by a health failure, is eventually admitted                                                                            |
| L3  | Transactions complete      | Every opened transaction eventually returns to idle                                                                                                                     |
| L4a | Refusal justified          | A request refused at once is refused only on a faulted manager transport                                                                                                |
| L4b | Operator requests complete | An accepted operator request eventually ends in a completed transaction or accounted tokens                                                                             |
| R1  | Proportionate rest         | After a clean ending the trigger is eligible within the settle units                                                                                                    |

_Table 3: Safety (S), liveness (L) and bounded-response (R) properties._

S8's reasons are a positive `cooling_for`, the drain fault with its guidance,
or the `manual` policy. A health failure refuses every queued request
(`Admission.health_failed`), so L2 treats that refusal as a terminal outcome,
and S7 keeps the supervisor quiescent afterwards. L2 also depends on the
queue's order: `Admission` serves it first in, first out, so the tracked
request waits behind at most the requests queued when it arrived, and later
submissions queue behind it. Given F5, no choice of selection is left to the
environment, and a busy queue cannot admit others for ever while the tracked
request waits.

L4 is split into a safety half and a liveness half, because the two fail in
different ways. L4a is a transition property of the refusal: a request refused
at once must have been refused because the manager transport is faulted.
`CacheRecovery.request` answers that fault with a `blocked` reason and keeps no
request pending[^8], so at `7445bf0` L4a holds by construction. At `eba08ad`
the request was also refused while a cached compile ran, and that is a
violation of L4a: it is the operator's half of incident 3, and it yields a
finite counterexample rather than a lasso. L4b is the liveness half: an
accepted request eventually ends in a completed transaction or accounted
tokens, which is what a request that waited out a busy queue must still
achieve. R1 captures a requirement that liveness cannot: only a faulted ending
imposes the cooldown. Table 4 maps each incident to the property it violates
and the counterexample predicted on its pre-fix behaviour.

| Incident         | Pre-fix behaviour                             | Property | Predicted counterexample                                                                             |
| ---------------- | --------------------------------------------- | -------- | ---------------------------------------------------------------------------------------------------- |
| 1. Livelock      | Stop guard counts every moving group          | L3, L1   | Lasso: stop retried as uncached groups overlap                                                       |
| 2. Idle cooldown | Cooldown after every ending; no `cooling_for` | S8, R1   | Finite: clean ending, new drain, no reason shown                                                     |
| 3. Capacity loss | Trigger and request rules at `eba08ad`        | L1, L4a  | L1 lasso: one slot retained, others cycle cached work; L4a finite: request refused under cached work |
| 4. Hidden fault  | Idle transaction drops its last fault         | S9       | Finite: restart refused to the limit, then idle                                                      |

_Table 4: Incidents as predicted counterexamples._

Two qualifications keep this claim honest. Incidents 1 and 3 are discovery
claims: L1, L3 and L4b say only that recovery makes progress, and nothing about
the guard or trigger that defeated it. Incidents 2 and 4 are
requirement-capture claims: S8, S9 and R1 would have failed only because the
model writes down what status must explain. The model makes that expectation
checkable; it does not invent it.

The incident 1 prediction is stated so that it can fail. With capacity 3 and
one retained token, two uncached groups can overlap indefinitely, so the stop
is never enabled and the counterexample exists under weak or strong fairness.
With capacity 2 the stop is enabled for an instant between one group's token
return and the next admission, so the counterexample exists only under weak
fairness of the recovery step. A result that inverts either prediction means
the model's atomicity is wrong.

The capacity-2 prediction is refined here, before the first run, because the
sketch's guard is not the whole of the code. `CacheRecovery._try_stop` is never
disabled: a blocked stop calls `_fail`, which re-arms the retry backoff and
resets the roots grace, so the next attempt is a new attempt at a re-armed
step. An adversary that expires the backoff only while an uncached group is
moving keeps the stop branch disabled at every attempt, and strong fairness
does not help, because it is the individual attempt that is enabled only
finitely often. Two configurations replace the single capacity-2 prediction:

- `incident-1-cap2-strong-nobackoff`, the RFC's original abstraction, in which
  the stop branch keeps no backoff. It is expected to hold under strong
  fairness, which is the prediction above.
- `incident-1-cap2-strong-backoff`, which models `_fail`'s re-arming. It is
  expected to yield a lasso under strong fairness.

Strong fairness stands in for a physical assumption the code does not
guarantee: an unsynchronized 1-second poll eventually lands in a window of
positive length. The H1 verdict will say so. Without the split, a lasso at
capacity 2 under strong fairness would be misread as wrong atomicity and would
spend W1's two-revision budget on the wrong cause.

### 4. Illustrative sketch

For screen readers: the following block is an illustrative fragment of the
model in pseudo-FizzBee. It declares state, the steps that lead a transaction
to its stop, one supervisor action with a switch that restores incident 1's
pre-fix guard, and four properties.

```plaintext
# Illustrative pseudo-FizzBee: unchecked; syntax is to be confirmed in W1.
CAPACITY = 3
PREFIX_GUARD_COUNTS_UNCACHED = False   # True restores incident 1's guard

action Init:
    # One token is already retained for the server. held_by_others counts
    # tokens other supervisors hold; held covers only this supervisor's own,
    # which partition into running, draining and server-drain groups.
    free, held, held_by_others, server_drain = CAPACITY - 1, 1, 0, 1
    running = {"cached": 0, "uncached": 0}
    draining = {"cached": 0, "uncached": 0}
    phase, manager_faulted, stop_issued = "idle", False, False

atomic action OpenTransaction:         # trigger conditions elided
    require phase == "idle" and server_drain > 0
    phase = "quiescing"

atomic fair action Quiesce:            # the barrier write, elided (F1)
    require phase == "quiescing"
    phase = "stopping"

atomic fair action RecoveryStop:       # a weakly fair poll step (F1)
    require phase == "stopping" and not manager_faulted
    require running["cached"] + draining["cached"] == 0
    require not PREFIX_GUARD_COUNTS_UNCACHED or \
        running["uncached"] + draining["uncached"] == 0
    stop_issued = True
    phase = "accounting"

transition assertion StopSafety(before, after):
    if not before.stop_issued and after.stop_issued:
        return before.running["cached"] + before.draining["cached"] == 0
    return True

always assertion TokensConserved:
    return free + held + held_by_others == CAPACITY

always assertion HeldPartitioned:
    return held == (sum(running.values()) + sum(draining.values())
                    + server_drain)

eventually always assertion RetainedDrainsAccounted:
    return server_drain == 0
```

FizzBee's documented constructs include `action Init`, `atomic` and `fair`
actions, `require` guards, `oneof` choice, `always`, `always eventually` and
`eventually always` assertions, and `transition` assertions over `before` and
`after`[^2]. Its documentation warns that copying shared state into a local can
introduce an interleaving point, and that some function calls must stand as
separate statements[^9]. The sketch therefore states the stop's guard with
`require` and reads state in place rather than through an assigned local: an
assignment enables an action even when the condition it computes is false, and
control flow alone does not disable one[^10]. A guard written as a local plus an
`if` would leave the action enabled in every state, which would let strong
fairness be met by a no-op and hide the capacity-2 counterexample. W1 must
still check the interleaving points this style leaves.

### 5. Keeping the model honest

A model that diverges from the code gives false confidence, which is worse than
none. Four mechanisms resist drift.

**Correspondence.** Each supervisor action transcribes one code path and cites
it, as Table 5 shows. An action is atomic exactly where the code runs within
one event-loop callback or one ordered tick sub-step.

| Model action    | Code                                        | Atomic unit                   |
| --------------- | ------------------------------------------- | ----------------------------- |
| Submit          | `Admission.submit`                          | Listener callback             |
| TokenArrives    | `Admission.token_arrived`                   | Pool callback                 |
| Cancel          | `Admission.cancel`                          | Connection callback           |
| LeaderExits     | `Groups.reap`, `Admission.leader_exited`    | `SIGCHLD` callback            |
| ScopeEmpties    | `Groups.advance`, `Admission.group_emptied` | Tick, before the server poll  |
| DrainProbe      | `ServerWatch.drained_tokens`                | Server poll, first            |
| RecoveryStep    | `CacheRecovery.tick`, one `_step`           | Server poll, after DrainProbe |
| OperatorRequest | `CacheRecovery.request`                     | Connection callback           |
| CacheRefresh    | `ServerWatch.refresh_cache_mode`            | Cache poll                    |
| HealthFails     | `Admission.health_failed`                   | Pool failure or device poll   |

_Table 5: Model actions and the code they transcribe._

**Mechanical drift check.** Each supervisor action carries a
`# maps: <dotted path>` comment. A pytest parses the model and fails if any
public event method of `Admission`, any `Effect` subclass, any `Phase` literal
or any fault kind is unmapped, or if a mapped path no longer resolves. It
cannot show that a transcription is right, only that nothing was added or
renamed silently.

**Known-bad configurations.** Each incident's pre-fix behaviour is a model
switch, such as `PREFIX_GUARD_COUNTS_UNCACHED`, and the check fails unless
every known-bad configuration yields a counterexample to its Table 4 property.
This mirrors the mutation evidence recorded in
`tests/test_cache_recovery_properties.py`: a model that stops finding the bugs
it was built to find has lost its discriminating power.

**Conformance.** Three checks, cheapest first:

1. _Counterexample replay._ Each trace is replayed through the production
   `Admission`, `ServerWatch` and `CacheRecovery` with the existing doubles
   (`FakePool`, `cache_recovery_harness`) before anything else changes. A trace
   the code follows is a bug: it becomes a scripted pytest that fails before
   the fix and passes after, and its pre-fix behaviour becomes a known-bad
   configuration. A trace the code cannot follow is a model defect, corrected
   and kept as a correspondence test. None is discarded. The trace is taken from
   `error-graph.json`, whose links carry the action name and the state the
   step reached, or from the `trace.txt` an `--experimental_no_graph` run
   exports; open question 3 records both forms. A replay must check the verdict
   line as well as the exit status, because every verdict exits 0.
2. _Projection inclusion._ The Hypothesis machines record each production
   transition projected onto the model's state, and each must be one the model
   permits. This checks the dangerous direction: a model that omits a
   production behaviour can hide a bug, whereas extra model behaviour only
   risks spurious counterexamples.
3. _Journal trace checking._ Supervisor records carry `queued`, `held`,
   `running`, `draining`, `server_drain` and `capacity`
   (`metrics.gauge_fields`) and every phase change, so a checker can confirm
   that a recorded sequence is a model path up to stuttering. `cached_moving`
   and the hold are not recorded, and rate limits may drop records, so a pass
   means "no contradiction found", not refinement.

**Update policy.** A pull request that changes `admission`, `admission_state`,
`effects`, `groups`, `group_drain`, `server_watch`, `sccache`, `cache_recovery`,
`recovery_starvation`, `recovery_barrier` or `supervisor_polling` must update
the model and its correspondence comments, or state why the model is
unaffected. Every model change runs the full check, known-bad configurations
included, before merge.

### 6. Location, tooling and runtime

These are proposals, subject to the spike's findings:

- `models/admission/` holds the model and one configuration per bound set and
  per known-bad variant; `docs/repository-layout.md` gains the path. The wheel
  is unaffected, because it packages only `buildlimits/`.
- The `Makefile` pins `FIZZBEE_VERSION` and a release checksum, installs the
  binary under the existing local-only `.tools/`, and adds `make model-check`
  for the routine bounds and every known-bad configuration.
- A CI job runs `make model-check` on pull requests that touch `models/` or
  the update-policy modules, and nightly with larger bounds, within proposed
  budgets of 10 and 60 minutes on a standard hosted runner.

## Hypotheses and decision gates

The thresholds below are proposals for approval before any trial runs.

### H1. Discovery

- **Assumption:** the abstraction of §1, under F1 to F7, preserves the
  behaviours behind incidents 1 and 3. F3a is not needed at W1's bounds:
  incident 1's stop is gated by cached work, not by the visible-roots grace,
  and W1 checks no configuration in which the grace is the only way forward.
- **Prediction:** at capacity 3, incident 1's known-bad configuration (the
  stop guard counting uncached groups) yields a lasso to L3, and incident 3's
  (the trigger and refusal at `eba08ad`) yields a lasso to L1 and a finite
  counterexample to L4a. The fixed configurations (PR #9's guard, and the
  trigger, opening and stop at `7445bf0`, from PRs #10 and #12) yield none. At
  capacity 2, incident 1's counterexample appears under weak but not strong
  fairness of the recovery step, and under strong fairness once the stop's
  retry backoff is modelled, as §3's refinement explains.
- **Comparator:** the Hypothesis recovery machine extended with uncached
  groups and a bounded-progress proxy: the stop is issued within N steps.
- **Falsified if:** a known-bad configuration passes after the one permitted
  revision of atomicity or fairness, or a fixed configuration fails with a
  trace that replay shows the code cannot follow.
- **Decision:** if not falsified, proceed to W2. If the comparator finds
  incident 1 at comparable cost with a defensible N, prefer extending
  Hypothesis and stop. If falsified, repeat the spike once in TLA+, or stop and
  record the negative result.

### H2. Tractability

- **Prediction:** the full model, at capacity 3 with two waiting requests,
  two abnormal endings, one manager fault and a restart limit of 2, checks
  every property within the pull-request budget.
- **Falsified if:** the smallest bounds that still reproduce every incident
  in Table 4 exceed the nightly budget.
- **Decision:** within budget, proceed to W4; over the pull-request budget
  only, run nightly; over both, split into a coupling model and a transaction
  model, re-measure once, then stop if still over.

### H3. Correspondence

- **Prediction:** projection inclusion and counterexample replay find no
  divergence that one correction to either side cannot resolve.
- **Falsified if:** a divergence persists after one correction round.
- **Decision:** make `make model-check` a required check only if H3
  survives; otherwise keep it advisory and record the gap.

### H4. The stale-path automatic stop

- **Question:** does L1 hold for the stale-path automatic stop, and under what
  assumption? At `37d08e5` a stale-opened transaction stopped only once the
  drain probe again proved zero visible roots; PR #12 added the 600-second
  visible-roots grace, so `7445bf0` stops without that proof once nothing
  cached is moving[^5].
- **Prediction:** at `37d08e5`, L1 holds with F3 and fails without it, through
  detached work that holds a visible root for ever, so the stop is never
  queued; L4a and L4b hold without F3. At `7445bf0` the grace timer expires
  under F6 and queues the stop, which closes that route. Expiring it also needs
  F3a: the grace runs only while the root reading is known, so an unreadable
  count at every attempt would hold the stop for ever. L1 then still needs the
  accepted stop to reach a verified stopped and empty service
  (`ManagedSccache.accounted_drain`). The model states that as part of F4, a
  completed stop job leaving an empty control group, or else keeps F3; it does
  not assume it silently.
- **Falsified if:** `37d08e5` satisfies L1 without F3; or `7445bf0` fails L1
  with F1 to F7, F3a and that reading of F4 in force. A grace that cannot
  expire, such as one broken by an unreadable root count at every attempt,
  would also falsify it; that is exactly what dropping F3a models.
- **Decision:** none here. The model checks the merged fix; it does not design
  it. If the `37d08e5` prediction holds, that revision becomes a fifth
  known-bad configuration.

## Workstreams

Table 6 proposes four workstreams in dependency order; the dependencies are
acyclic.

| ID  | Outcome                                                                                          | Requires          | Exit evidence                                                                          | Gate                             |
| --- | ------------------------------------------------------------------------------------------------ | ----------------- | -------------------------------------------------------------------------------------- | -------------------------------- |
| W1  | Minimal model and busy queue; known-bad and fixed configurations for incidents 1 and 3           | None              | H1 verdict, traces replayed, open questions 1 to 6 and 8 answered                      | G1: proceed, change tool or stop |
| W2  | Full scope; S1 to S9, L1 to L3, L4a, L4b and R1; known-bad configurations for all four incidents | W1 at G1          | All properties hold at `7445bf0` under F1 to F7, plus F3a; known-bad fail; H4 answered | G2: bounds and CI budget         |
| W3  | Drift check, counterexample replay and projection inclusion as pytest suites                     | W2                | H3 verdict; every counterexample has a regression or correspondence test               | G3: required or advisory         |
| W4  | Pinned tool, `make model-check`, CI job, update policy and layout entry                          | W2; W3 to require | Within H2's budget; identical results on unchanged sources                             | G4: place in the quality gates   |

_Table 6: Proposed workstreams, their evidence and the decisions they enable._

W1 excludes health, start-up, escalation detail and metrics, and is limited to
two model revisions after its first full run; it stops at H1's falsification
condition. At `eba08ad`, W2 expects L1 and L4a to fail, because that revision
is incident 3's pre-fix behaviour; at `37d08e5` it expects L1 to hold only
under F3, and at `7445bf0` it expects L1, L4a and L4b to hold. W3 may add
journal trace checking as an optional tool.

## Risks and mitigations

| Risk                     | Mitigation                                                                 |
| ------------------------ | -------------------------------------------------------------------------- |
| State explosion          | Counts by class; minimal bounds that reproduce Table 4; split models (H2)  |
| Model drift              | Drift check, update policy, known-bad configurations, projection inclusion |
| False confidence         | Every report names its fairness assumptions and conformance limits         |
| Generous fairness        | F1 is weak, not strong; F2 to F5 reviewed against systemd and sccache      |
| Spurious counterexamples | Replay before triage; one correction round per trace                       |
| Tool immaturity          | Pinned version; workarounds recorded in W1[^9]; TLA+ fallback              |
| Lost model knowledge     | Python-like notation, correspondence comments, developer's guide section   |

_Table 7: Risks and their mitigations._

## Compatibility and migration

Users see nothing: no runtime dependency, configuration, behaviour or artefact
changes, and the wheel's contents stay the same. Maintainers gain a pinned
tool, a `Makefile` target, a CI job and an obligation to keep the model in step
with the update-policy modules; until G3 that obligation is advisory and the
check does not block merges. Each workstream is useful on its own, and stopping
at any gate leaves the repository no worse than today, with the evidence
recorded.

## Alternatives considered

### Option A: TLA+ or PlusCal with TLC

TLA+ is the established notation for this kind of model, with mature weak and
strong fairness, a widely used explicit-state checker and a large body of
practice. Its notation is further from the Python codebase, which raises the
cost of review and of an honest correspondence. It is the fallback if FizzBee
fails W1.

### Option B: P

P models communicating state machines, checked by systematic testing with
monitors for liveness. It suits message-passing actors better than one loop
over shared observations, and would bring a .NET toolchain into a Python
repository.

### Option C: extend the Hypothesis state machines

This is the cheapest option and is partly adopted anyway: uncached groups, a
joint admission-and-recovery machine and varied time steps would all strengthen
the suite. Random search over finite prefixes cannot establish liveness,
however, only a bounded-progress proxy whose bound must be chosen. W1 measures
this option directly as H1's comparator.

### Option D: bounded model checking of the implementation

Kani-style bounded checking verifies compiled Rust; no comparable tool targets
Python. CrossHair, already in use, checks function contracts symbolically but
has no notion of interleaving or temporal properties.

### Option E: retain the current verification

The four incidents are the cost of this option. Each was fixed with a targeted
test after the fact; more tests of the same kind would not have predicted them.

| Approach             | Liveness and fairness | Exhaustive in bounds   | Notation distance | Toolchain                  |
| -------------------- | --------------------- | ---------------------- | ----------------- | -------------------------- |
| FizzBee              | Yes                   | Yes                    | Low               | Release binary, to confirm |
| TLA+ with TLC        | Yes                   | Yes                    | High              | Java                       |
| P                    | Monitors              | No, systematic search  | Medium            | .NET                       |
| Hypothesis extension | Bounded proxy only    | No, random search      | None              | Existing                   |
| CrossHair            | No                    | Symbolic, per function | None              | Existing                   |

_Table 8: Verification approaches compared._

## Open questions

These are to be settled by evidence, mostly in W1, rather than assumed:

1. **Settled: see Open question 1 below.** FizzBee's syntax for records and
   collections in state, and whether fairness can apply per instance or
   parameter (each group, each timer) rather than per action, for example
   through roles.
2. **Settled: see Open question 2 below.** The liveness-checking modes and how
   lasso counterexamples are reported; documented checker output shows a
   `liveness: "strict"` option whose meaning is unconfirmed[^2].
3. **Settled: see Open question 3 below.** Whether traces and state graphs can
   be exported in a machine-readable form for replay and projection inclusion.
4. **Settled: see Open question 4 below.** Packaging of the Linux release:
   runtime dependencies, checksums or signatures, and pinning. Prebuilt Linux
   binaries are documented[^2].
5. **Settled: see Open question 5 below.** The licence of the pinned release.
   The repository page listed Apache-2.0 when read[^11]; confirm it for the
   pinned release before adoption.
6. **Settled: see Open question 6 below.** Performance at H2's bounds, and
   whether symmetry or partial-order reduction is available.
7. Whether FizzBee's model-based testing tool, whose quick start targets
   Go[^12], can drive Python; if not, W3's replay stays bespoke.
8. **Settled: see Open question 8 below.** The exact pre-fix revisions for
   incidents 1, 2 and 4, which predate the import into this repository.
   Known-bad configurations must transcribe those revisions, not a recollection
   of them.
9. Whether to add `cached_moving` and the hold to the metric gauges, a code
   change for a separate decision that would let journal trace checking see the
   variables behind incidents 1 and 3.
10. Whether `make model-check` joins `make all`, and whether the CI job
    becomes required after G3.
11. Whether the supervisor journal records from 2026-10-18 are retained, so
    that the incidents can serve as trace-checking fixtures.
12. Whether supervisor crash and restart, with start-up accounting and
    barrier reclamation, belong in W2 or a later revision.

## Open questions record

This section records the evidence that settles the open questions above. Each
entry states the question, the answer, and the evidence for it. An entry is
added when the evidence exists, not when the question is raised.

Open questions 1, 2, 3 and 6 are settled below; 4, 5 and 8 were settled in
earlier entries in this section. Questions 7 and 9 to 12 stay open: none bears
on W1, and each waits on evidence a later workstream produces.

Three entries are neither open question nor settled answer, and are recorded
here because they change what §2, §3 and H1 predict: the capacity-2 refinement
(RC1), the split of L4 and the addition of F3a (RC2), and a correction to an
earlier misreading of weak fairness (RC3). The first two are the RFC's side of
deviations DV2 and DV3, recorded before W1's first run as the plan requires;
the third is a finding of the spike.

### RC1: the incident 1 capacity-2 prediction

§3 now carries the refinement inline, beside the prediction it amends, and
names the two capacity-2 configurations and what each is expected to show. In
short: the stop within one poll step is weakly but not strongly fair in an
abstraction without the retry backoff, and `CacheRecovery._try_stop` re-arms
that backoff through `_fail`, so strong fairness is met only if the poll lands
in the retry window. The model therefore splits `RecoveryStopTaken` from
`RecoveryStopBlocked` and checks `incident-1-cap2-strong-nobackoff` and
`incident-1-cap2-strong-backoff` separately.

Why this is not a model convenience: `_fail` re-arms
`min(RECOVERY_RETRY_SECONDS * attempts, RECOVERY_MAX_RETRY_SECONDS)` and resets
the roots grace, so the blocked attempt is a step that recurs with a new
deadline rather than a dead branch. Without the split, a lasso at capacity 2
under strong fairness would be read as wrong atomicity, and W1's two-revision
budget would be spent on the wrong cause. Strong fairness in the backoff
configuration stands in for a physical assumption the code does not guarantee:
an unsynchronized 1-second poll eventually lands in a window of positive
length. The H1 verdict must say so.

### RC2: L4 is split, and F3a is added

L4 is now L4a (safety: a request refused at once is refused only on a faulted
manager transport) and L4b (liveness: an accepted request eventually ends in a
completed transaction or accounted tokens). Incident 3's finite counterexample
is a counterexample to L4a; `CacheRecovery.request` keeps no request pending
when it refuses[^8], so L4b is the half that has to make progress. In Table 3,
Table 4, W1's and W2's exit evidence and H4, every reference is now to the half
it means.

F3a (`unknown root readings are finite`) is stated beside the drain probe,
because `recovery_roots.stop_verdict` reaches `deadline` only once
`VisibleRootsGrace.observe` has seen roots as visible on consecutive attempts,
and an unreadable count is neither visible nor proven zero. Configuration
`head-unknown-roots`, with F3a dropped, is expected to fail L1; that is H4's "a
grace that cannot expire" clause made checkable. W1 does not depend on F3a,
because incident 1's stop is gated by cached work rather than by the grace.

### RC3: weak fairness does force a continuously enabled action

An earlier reading of the spike concluded that weak fairness did not force a
continuously enabled action to run, and that conclusion was wrong. It came from
a model with an unbounded counter whose exploration `max_actions` truncated: at
the cut bound the fair action appeared to be ignored, and at a larger bound it
ran. The bounded control is `models/spike/continuously_enabled.fizz`, which
passes under `fair`, and `continuously_enabled_unfair.fizz`, which fails on a
stutter from the initial state. F1's use of weak fairness for the supervisor
poll therefore stands, and the truncation entry below records the trap in
general form.

### Open question 1: state values, roles and fairness granularity

**State values.** Records, dicts, sets, bags and lists are all state, and the
checker renders each back in the state label. `models/spike/collections.fizz`
writes a record, a dict, a set, a bag, a list and a record nested inside a
dict, and the graph it leaves behind carries
`{"slots":{"running":1,"waiting":0}}`, `{"multi":["x"]}`,
`{"pairs":[{"at":1,"kind":"cached"}]}`, `{"seen":["cached"]}` and
`{"by_name":{"only":{"n":1}}}`. A field of a record can be assigned in place
(`slots.running = slots.running + 1`), and the record is re-rendered with the
new value, so the admission model can keep the attribute-style grouping the
Python code uses without rebuilding the whole record on each write.

**Fairness is per action, and per instance where a role or a choice makes
one.** Three spellings give progress for each element of a collection, and all
three pass:

- `atomic fair action Tick` inside a `role`, with two instances in a `bag()`
  (`roles_fairness.fizz`). The compiled fairness is attached to the action, and
  the checker applies it to each instance;
- `fair oneof` over a bag of roles (`roles_fairness_fair_oneof.fizz`), which
  gives 8 explored nodes, 8 valid and 4 distinct states;
- the same model without the modifier (`roles_fairness_unfair.fizz`), which
  fails, so fairness and not the shape of the model decides the verdict.

**What the model must not do with locals.** An assignment is a yield point and
therefore an interleaving point, and an action is enabled by its assignments
whatever a later test decides. The matrix below is the evidence, run on a gate
`v` that `Step` keeps flipping, with `Deliver` obliged to deliver only when the
gate is open. Every cell is a real run of the pinned checker.

| `Deliver` body                      | Atomic action | Plain action              |
| ----------------------------------- | ------------- | ------------------------- |
| `seen = v` then `if seen == 1`      | Fails         | Fails, at 20 nodes        |
| `if v == 1` in place                | Passes        | Passes                    |
| `seen = v` then `require seen == 1` | Passes        | Fails, at 20 nodes        |
| `require v == 1` first              | Passes        | Passes                    |
| `done = True` then `require v == 1` | Passes        | Reaches the goal, 8 nodes |

_Table 9: `Deliver` spellings against atomicity, all measured under
`always eventually done`._

The last row is the one that misleads if only the liveness verdict is read. In
the plain action it _passes_ `always eventually done` at 8 nodes, because the
goal is reached; what it also does is reach `done` true with `v` zero, a
delivery through a closed gate. Adding the safety assertion
`not done or v == 1` to the same plain model fails it at 3 nodes, while the
atomic spelling still passes, and the counterexample is
`Init {done: false, v: 0}` → `Deliver {done: true, v: 0}`.

So a guard placed after a commit is invisible to a liveness property and only a
safety property catches it. W1's models therefore state each interleaving
hazard as a safety assertion over the reachable state, and use liveness only
for the progress properties it is meant for. That pair is
`interleaving_plain_rollback.fizz` (fails) against
`interleaving_assign_require.fizz` (passes).

Read the table as three rules.

1. The hazard is a test after an assignment, in either kind of action. Copying
   the gate into a local and then testing the local fails in both.
2. `require` rolls an action back to its start only in an atomic action. In a
   plain action each assignment commits and yields as it runs, so a later
   failing `require` leaves the earlier write standing. The last row of the
   plain column reaches the goal in 8 nodes instead of a rollback's 4, and the
   state it reaches includes a delivery through a closed gate — which the
   liveness property cannot see and the safety assertion fails at 3 nodes.
   `models/spike/interleaving_plain_rollback.fizz` is that model, and its
   counterexample is `Init` then `Deliver`, ending at `{"done": true, "v": 0}`.
3. Reading state in place, or putting `require` first, is safe in both kinds.
   Both spellings the RFC sketch proposes therefore hold, and the sketch's
   reasoning was incomplete rather than wrong: the rule it states is about
   guarding before a commit, and only an atomic action supplies that guarantee
   for free.

The model in W1 is written with atomic actions wherever the code runs within
one callback or one ordered tick sub-step, which Table 5 already requires, and
reads state in place inside every guard. The plain-action column is a warning
about any step the model admits as non-atomic, not a style choice.

**A plain action's yield is visible in the error graph.** When a plain action
writes twice, the counterexample carries the action's own link and then a
second link named `thread-0` with `Type` empty, holding the intermediate state.
The admission model's steps are atomic, so it has none, but a replay that reads
links positionally must tolerate them.

**Weak fairness does force a continuously enabled action.** This entry corrects
an earlier reading of the spike. A bounded model with a sticky flag that never
disables passes under `fair` (`models/spike/continuously_enabled.fizz`), and
the same model with both actions unfair fails on a stutter from the initial
state. The earlier conclusion that weak fairness was ineffective came from an
unbounded counter whose exploration the `max_actions` bound truncated, which is
the truncation artefact recorded under open question 2. Weak fairness is the
right assumption for the supervisor poll, and F1 stands.

**A no-op action still discharges fairness.** An action that assigns a value
equal to the one already there is enabled in every state and counts as a step:
`atomic fair<strong> action Step: done = done` in a liveness model passes the
property through the empty cycle. The RFC's illustration calls this out as a
reason to write guards with `require`, and the measurement agrees, so a guard
written as a local plus an `if` is not merely imprecise but can hide a genuine
lasso.

### Open question 2: liveness modes, counterexample shapes and verdicts

**Which liveness modes check what.** The frontmatter key is a string, and the
checker dispatches on it in `main.go` (v0.5.4, the `CheckStrictLiveness` and
`CheckFastLiveness` branches). Table 10 records every spelling the spike ran,
with the number of `Checking` lines it printed.

| `liveness:` value                                 | Checker                 | `Checking` lines | Verdict               |
| ------------------------------------------------- | ----------------------- | ---------------- | --------------------- |
| absent                                            | Strict                  | 1                | As strict             |
| `"enabled"`, `"true"`, `"strict"`, `"strict/bfs"` | Strict                  | 1                | As strict             |
| `"eventual"`, `"nondeterministic"`                | Nondeterministic (fast) | 2                | Different algorithm   |
| `"none"`, `"false"`, `"null"`, `"yes"`            | None                    | 0                | `IsLive: true` anyway |
| any other string                                  | None                    | 0                | `IsLive: true` anyway |
| unquoted YAML `true`                              | Compile failure         | none             | Exit 1                |

_Table 10: Every `liveness:` the spike ran and what it did._

The last two rows are the traps. An unrecognized string checks nothing and
still prints `IsLive: true`, so a misspelt mode reads exactly like a pass; and
an unquoted YAML boolean is a frontmatter parse error
(`invalid value for string field liveness: true`) with exit status 1, not a
silent default. Every model in the repository states the key as a quoted
string, and a configuration that turns liveness on names the mode rather than
relying on the default. `liveness_eventual.fizz` prints
`Checking strict liveness with nondeterministic checker`, which is how the
spike distinguishes the two algorithms in captured output.

**The liveness setting is copied verbatim into `state_config.json`.** Each run
directory holds `state_config.json` with the parsed options, and the `liveness`
key repeats the frontmatter string exactly. When the frontmatter omits the key,
the key is absent from `state_config.json` even though strict liveness still
runs, so the file records what was asked for, not what was done. The report
must take the mode from the model or from the printed `Checking` lines, never
from this file alone.

**Fairness levels.** An action is `UNFAIR` with no modifier, `WEAK` for `fair`
or `fair<weak>`, and `STRONG` for `fair<strong>`; `Init` is `STRONG` because it
is the constructor. The levels are visible in the compiled AST the launcher
writes beside the spec (`actions[].fairness.level` in `<spec>.json`), which is
how `fairness_levels.fizz` is checked. Weak fairness forces an action that
stays enabled; it does not force one that is enabled only intermittently, which
needs strong fairness. `weak_fairness.fizz` and `strong_fairness.fizz` are the
pair that separates the two.

**A lasso and a stutter are different shapes.** A lasso's error graph ends
where it began through named actions (`Init`, `Gate`, `Gate` for
`liveness_strict.fizz`), and its links carry weak fairness. A stutter's second
link is named `stutter`, carries fairness 0 and repeats the first state, which
is what `liveness_stutter.fizz` produces and what a truncated run looks like.
The distinction matters because a stutter is often the bound rather than the
model; see the truncation entry below.

**Verdicts go to standard output, not to the exit status.** Every verdict —
safety failure, deadlock, liveness failure, and a pass — exits 0, and only a
compile or parse failure exits 1. The failure lines are distinct
(`FAILED: Model checker failed. Invariant:  NeverFull`,
`FAILED: Model checker failed` after `DEADLOCK detected`, and
`FAILED: Liveness check failed`), and the spike asserts the line rather than
the status. A runner that checked only the exit code would report every
counterexample as a success.

### Open question 3: machine-readable graphs and traces

**What a run writes.** A passing run writes `graph.dot`, `nodes_*.pb`,
`adjacency_lists_*.pb`, `spec_ast.json` and `state_config.json` into
`out/run_<timestamp>/`, with `out/latest` a symbolic link to the newest. A
failing run adds `error-graph.dot`, `error-graph.json`, `error-states.html` and
`nodes_errors.pb`. Eight output classes are captured in
`tests/fixtures/fizzbee/`, one directory each, with the standard output, the
exit status and the graphs: the five `error-graph.dot` files are byte-identical
to what the checker wrote, the five `error-graph.json` files are pinned by
`test_captured_error_graphs_match_the_live_ones`, and the seven `graph.dot`
files differ from a fresh run only in their pointer node ids. Only the standard
output needs masking, for the fields listed under determinism below.

**`error-graph.json` is the replayable form.** It is a JSON list of links whose
keys are exactly `Node`, `Type`, `Name`, `Labels`, `Fairness`, `ChoiceFairness`,
`Messages`, `ReqId`, `ThreadsMap`, `FailedInvariants` and `Returns`, asserted
by `test_error_graph_link_schema`. `Name` is the action that produced the step,
`Type` is `action` for a real step and the empty string for `Init` and for a
stutter, `Fairness` is 0 for a stutter and otherwise the action's level in the
order 1 unfair, 2 weak, 3 strong, and `Node.state` is the model state. That is
enough to project a counterexample onto the model's state and to replay it
against the production code, which is what §5's conformance check needs.

**`spec_ast.json` is the compiled model.** It is `json.load`-equal to the
`<spec>.json` the launcher writes beside the specification, and its top-level
keys are `sourceInfo`, `invariants`, `actions` and `frontMatter`. The action
list carries each action's name and fairness level, so the drift check can read
the model's fairness from the checker's own parse rather than by re-parsing
FizzBee source.

**Node ids differ between the two dot files.** In `graph.dot`, whether the run
passed or failed, nodes are identified by Go pointers (`"0x25387d7ac000"`) and
no two runs agree; in `error-graph.dot` they are small integers (`"0"`, `"1"`)
and they do. Any comparison of a `graph.dot` across runs must mask the ids, and
any snapshot of one must mask them too, which is why the spike compares counts
and state labels instead.

**Traces exist, behind an experimental flag.** `--experimental_no_graph`
refuses specifications with liveness assertions, disables crash-on-yield
simulation and turns on the processed queue; in exchange a failing run prints
`Trace (3 steps):` with the action names, writes `trace.txt` beside the run
directory's other files, and replays the trace itself with full per-step state.
The file is one action name per line, and `--trace-file` re-runs it: the spike
fed the exported file back and got the same safety failure. A trace file that
does not exist is not an error either — the checker prints
`Error parsing trace file: failed to open ...` and exits 0 — so a replay runner
must check for the file and for the verdict rather than trust the status.
Without the experimental flag, `--trace-file` expects a file the user supplied,
and the ordinary counterexample has to be reconstructed from `error-graph.json`.

### Open question 6: performance, symmetry and reduction

**Symmetry reduction is on by default and applies to `bag()` membership.** Two
symmetric workers in a bag give 9 explored nodes, 7 valid and 3 distinct
states; the same model under `--no-symmetry-reduction` gives 12, 10 and 4. A
`list` of the same roles gives 12, 10 and 4 either way, because list order
keeps the identities concrete, which is what the launcher's help text warns
about. The flag is only recognized **before** the specification's file name:
the launcher's argument loop stops at the first non-option argument, so
`fizz spec.fizz --no-symmetry-reduction` runs with the default reduction and
reports the reduced counts. Any configuration that needs the flag must place it
first.

**There is no partial-order reduction.** Neither the launcher's help nor the Go
source mentions one: the only reduction switch is the symmetry flag, and the
source has no partial-order machinery. The state counts at H2's bounds are
therefore a plain product of the concurrent components modulo symmetry, and the
small bounds Table 3 and Table 4 need are the only lever available.

**Cost.** A toy model of four nodes takes about 0.6 s of wall clock, of which
about 1 ms is model checking; a 140-node toy takes about 0.8 s. The launch
overhead of the bundled parser dominates at these sizes, so a configuration
list of a few dozen models is comfortably inside the routine budget, and the
nightly budget is the one to watch as the bounds rise.

### Truncation: a bound that changes the verdict

`max_actions` cuts exploration, and the checker says nothing about it. At
`max_actions: 2`, `truncation_safety.fizz` passes although the level-3 state it
forbids is reachable, and the same model at a larger bound fails; at
`max_actions: 6`, `truncation_liveness.fizz` fails with a `stutter`-terminated
lasso that the larger bound does not produce. A pass at a bound and a
stutter-terminated liveness failure are therefore both suspect until the state
count has stopped growing with the bound. Every configuration in the model
check records the count it reached, and the expectation judgement treats a
stutter from a run whose count is still rising as inconclusive rather than as a
counterexample.

### Determinism: what repeats and what does not

Five runs of a failing safety model and five of a passing symmetry model, at
`GOMAXPROCS` 1 and at `GOMAXPROCS` 4 with `GOMEMLIMIT=4GiB`, gave one distinct
normalized standard output per model, exit 0 every time, one distinct
`error-graph.json`, and pointer-masked `graph.dot` hashes equal across both
concurrency settings. The figures that do vary — `elapsed`, `queued` and the
timestamps in directory names — are masked by `tests/fizzbee_spike.py`. Node
ids in `graph.dot` are pointers and vary between runs even at one setting, so
they are masked rather than compared. The pinned checker's exploration order is
deterministic at these sizes; a report may therefore compare two runs for
equality, provided it masks those fields first.

### Open question 4: packaging, dependencies and pinning

**Release and digest.** The pinned release is `v0.5.4`, published on 2026-10-01
at <https://github.com/fizzbee-io/fizzbee/releases/tag/v0.5.4>. The tag object
is unsigned: GitHub's tag API reports `verification.reason` as `unsigned`, so
there is no signature to check. The integrity evidence the release does publish
is a digest per asset, and the digests below are the ones this project pins.

For screen readers: the following block lists the digest of the release tarball
in use and the digests of the container image recorded as a fallback.

```plaintext
tarball  linux_x86  sha256:8a1d0900f9ad920002151922337912e11f9e588a2f1196249fe40bf75370f782
image    index      sha256:e7b90a0157ab466283e96a9650ab9f91b8ab80a9db8ac6f1d868ecd2b2c687b8
image    amd64      sha256:c0ecb5c6142553c86ffd4f99c10e5186385056489f124952d33c76f1dc1216db
image    arm64      sha256:5503b7be21acadb1906ee4307e4eff3555b0217d2bbd706c1e2c2578550fc5c4
```

The tarball digest is **trust-on-first-use**, recorded on 2026-10-24: it was
compared with the hash of the downloaded 335 MiB asset and with the digest the
release page publishes, and with no signature, because none exists. The release
publishes three further assets for ARM and macOS, each with its own digest;
none of them is pinned.

**Tarball contents.** The tarball holds 1,795 entries: 1,673 files and 122
directories, with no symbolic links. Unpacked they occupy about 675 MiB. Table
10 groups them by area.

| Area          | Entries     | Unpacked | Purpose                                                                    |
| ------------- | ----------- | -------- | -------------------------------------------------------------------------- |
| `fizz`        | 1 file      | 18 KiB   | Bash launcher: reads `fizz.env`, runs the parser, then the checker         |
| `fizzbee`     | 1 file      | 8.5 MiB  | Go model checker, stripped, linked only against libc                       |
| `fizz.env`    | 1 file      | 121 B    | Sets `PARSER_BIN`, `FIZZBEE_BIN` and `MBT_GEN_ZIP`                         |
| `mbt_gen.zip` | 1 file      | 169 MiB  | Zipapp for model-based-test scaffolding, with Jinja2 and MarkupSafe        |
| `parser/`     | 1,669 files | 497 MiB  | Bazel stub and runfiles tree: CPython 3.12.11, the ANTLR runtime, protobuf |
| total         | 1,673 files | 675 MiB  | The unpacked cache entry                                                   |

_Table 11: Contents of the v0.5.4 Linux x86 release tarball._

The member listing is reproducible from the pinned asset with `tar -tzf`, and
the digest above fixes which bytes it names, so this census and the licence
inventory below stand in place of a 1,795-line dump.

**Runtime dependencies.** The launcher is bash and calls `dirname`, `mktemp`,
`readlink` and `rm`. The checker needs no shared object beyond libc:
`ldd fizzbee` lists only `linux-vdso`, `libc` and the ELF interpreter, and
`go version -m` reports `go1.26.7`. The parser stub begins
`#!/usr/bin/env python3`, so a `python3` must be on `PATH`; the stub then
re-executes the bundled CPython 3.12.11 by a hard-coded runfiles path, so the
system interpreter only has to run the stub itself. The spike tested CPython
3.12.15 on Rocky Linux 10. With no `python3` on `PATH` a run stops at once with
`/usr/bin/env: 'python3': No such file or directory`,
`Error: Compilation failed` and exit status 1. The launcher's `mbt-scaffold`
subcommand also needs an unversioned `python`; model checking never invokes it.
A run needs no network. It writes its graph files to `--output-dir` when one is
given, defaulting to `SPEC_DIR/out/run_<timestamp>`, and it always writes
`<name>.json` beside the specification it reads, which is why every run here
copies the specification into a scratch directory first.

**The container image.** This plan named `ghcr.io/fizzbee-io/fizzbee:v0.5.4` as
the fallback pin. That tag does not exist: the registry lists `0.5.3`, `latest`
and `0.5.4`, with no `v` prefix. The corrected pin is the index digest in the
block above, created on 2026-10-03, which both `0.5.4` and `latest` resolve to.
The `Makefile` records it as `FIZZBEE_IMAGE`, a fallback that no target pulls
or runs: the tarball needs no container runtime, and adding Podman to the
quality gates is not part of W1.

Two properties of the image are worth the record. It wraps the same release —
entrypoint `/opt/fizzbee/fizz`, `/opt/fizzbee` first on `PATH`, and CPython
3.12.15 at `/usr/local/bin/python3` — so it satisfies the `python3` dependency
itself. And its amd64 attestation is an in-toto statement carrying the SLSA v1
provenance predicate: BuildKit as the build type, GitHub Actions run
37125967911 attempt 1 as the builder, source revision `579328a5`, and
`pkg:docker/python@3.12-slim-trixie` as the resolved base image. The tarball
publishes nothing comparable, so the image carries stronger supply-chain
evidence than the artefact in use; recording that asymmetry is the limit of
what the spike can verify. The release notes say the image is rebuilt monthly
on the current Debian trixie base, so only the digest is a stable pin, never
the tag.

**The no-network check.** Rootless Podman 5.8.2 ran a two-state toy model from
a bind mount with `--network=none`, and it printed
`PASSED: Model checker completed successfully`. DNS resolution inside that
container failed, and the same model passed again with `--network=host`, so the
pass does not depend on the isolation.

### Open question 5: the licence of the pinned release

The upstream repository declares Apache-2.0 (GitHub's licence API returns
`spdx_id: Apache-2.0`, read 2026-10-24), and that is the licence of the pinned
release. The artefact does not carry the licence: no Apache-2.0 text and no
`NOTICE` file appears anywhere in the tarball, and the Go checker is stripped,
so `go version -m` prints only its toolchain and its carriage of notice text
cannot be checked either. The licence rests on the repository declaration,
which is what roadmap task 1.1.3 asks to record.

What the tarball does ship is a set of third-party components whose notices
travel with them. Table 12 lists them with the licence text each one brings.

| Component               | Version | Licence                    | Licence text in the release |
| ----------------------- | ------- | -------------------------- | --------------------------- |
| FizzBee Go checker      | v0.5.4  | Apache-2.0 (repository)    | None                        |
| CPython runtime         | 3.12.11 | PSF                        | `LICENSE.txt`               |
| Tcl/Tk                  | 8.6     | BSD-style                  | `license.terms`             |
| antlr4-python3-runtime  | 4.13.1  | BSD (its `METADATA`)       | None                        |
| protobuf Python runtime | 6.33.0  | Not stated in the artefact | None                        |
| pip                     | 24.3.1  | MIT                        | `LICENSE.txt`               |
| Jinja2                  | 3.1.6   | BSD-3-Clause               | `LICENSE.txt`               |
| MarkupSafe              | 3.0.2   | BSD-3-Clause               | `LICENSE.txt`               |

_Table 12: Components in the v0.5.4 release and their licences._

Every row marked "None" was searched for and not found. The project does not
redistribute the release, so these obligations stay with the downloader: the
cache is local and git-ignored, and `.tools/fizzbee` is a git-ignored symbolic
link into it. Should a later step ship the checker inside a container image,
that image must take on the Apache-2.0 text and the bundled notices together.

### Open question 8: the pre-fix revisions

Incidents 1, 2 and 4 predate the import into this repository, so the question
is whether their pre-fix behaviour survives in any history at all.

**The dev-env-rocky history does not name it.** The package was vendored there
whole in `d83f709` ("Add bounded recovery for starvation by detached cached
work", 2026-10-14) and removed whole in `0eeebbe` (2026-10-19). Between those
commits its source modules were never touched: the only changes under
`python/build_limits/` are test-quality repairs in `0e60b80` and `5e1ee52`.
Pickaxe searches of that history add nothing: `RECOVERY_COOLDOWN_SECONDS` and
`_last_fault` appear only in the vendoring commit, and `cached_moving` and
`cooling_for` appear nowhere at all. The vendored copy at `d024aaa`, the last
commit that carries it, shows the pre-fix behaviour of all three incidents —
one 900-second cooldown, no `cached_moving`, and an idle branch that drops
`last_fault` — but it is a single snapshot, not three revisions.

**The in-repository history names it.** PR #9 was squashed, but the staging
branch `initial-import` keeps its individual commits, and that branch's tip
`416b1ae` is tree-identical to the squashed import `eba08ad`:
`git diff eba08ad initial-import` is empty. The revisions below are read from
that branch, which is therefore the exact pre-squash history of the imported
code rather than a reconstruction.

| Incident         | Last pre-fix commit | Clauses it contributes                                                                                                                                                                       | Rest of its diff                                        |
| ---------------- | ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------- |
| 1. Livelock      | `9f2c94f`           | `blocked` refuses while `counts.running or counts.draining` and then requires `held == server_drain`; the trigger's `_every_held_token_awaits_the_server` requires the same equality         | Spelling fixes in four files, none in a recovery module |
| 2. Idle cooldown | `f1ee9aa`           | `_finish` always sets `now + RECOVERY_COOLDOWN_SECONDS`; `describe` has no `cooling_for`; the idle branch of `_recovery_lines` returns no line                                               | A migration guide, `docs/contents.md` and the README    |
| 3. Capacity loss | `eba08ad`           | The trigger needs `zero_visible_roots` and no cached group moving in one reading, with no stale proof; `_try_stop` requires the zero-roots proof; `request` is refused while `blocked` holds | The imported package itself                             |
| 4. Hidden fault  | `d91cd22`           | The idle branch of `status_render._recovery_lines` drops `last_fault`, so a faulted ending renders as a clean one                                                                            | Tests for the recovery rests                            |

_Table 13: Pre-fix revisions of the four incidents._

The five files the incidents turn on — `admission`, `admission_state`,
`cache_recovery`, `recovery_starvation` and `status_render` — are unchanged
between `a2f2413` and `9f2c94f`, so the import itself is an equally valid
transcription point for incident 1. Each fix is the commit immediately after
the revision named:

- Incident 1: `846b1e3` adds `Counts.cached_moving`, makes `blocked` read it,
  replaces the trigger clause with `_the_server_alone_holds_cached_work` and
  drops the `held != server_drain` clause. Its 14-file diff changes one
  production behaviour — the new snapshot field and the clauses that read it —
  beside comments, documentation and tests.
- Incident 2: `ab6796e` adds `cooldown_after` (900 seconds after a refused
  restart, 60 after a clean one), reports `cooling_for`, and renders the rest.
- Incident 4: `416b1ae` retains `last_fault` in the idle branch.
- Incident 3: `37d08e5` and `7445bf0`; see "Production incidents" above.

`846b1e3` is not an ancestor of `main`. PR #9 was squashed, so `main` carries
the pre-fix and fixed states in one commit, `eba08ad`. That is why the
known-bad configurations transcribe revisions from `initial-import` rather than
annotating `main`.

**Consequence for W1.** The known-bad configurations name `9f2c94f`, `f1ee9aa`
and `d91cd22` in their `# transcribes:` comments. The vendored `d024aaa`
snapshot is recorded beside them as corroboration, not as a revision: it agrees
with `a2f2413` except for the import's mechanical changes — `from __future__`
removal, `build_limits` to `buildlimits` renames, docstring paths, and the
`_section` helper added to `status_render`.

## Recommendation

Approve W1 now and decide the rest at G1. The incidents expose a class of
defect, progress under a busy and adversarial environment, that the current
verification cannot express, and FizzBee offers liveness checking under
explicit fairness in a notation close to the code. The spike tests that claim
cheaply and decisively. If FizzBee cannot rediscover incidents 1 and 3 on their
pre-fix behaviour, the RFC should be withdrawn or redirected to TLA+ or to
extending Hypothesis, and that result recorded.

## Acceptance

Accepted on 2026-10-18. Acceptance approves the recommendation above: W1 goes
ahead now, with the H1 to H4 thresholds as written, and W2 to W4 proceed only
as each gate, G1 to G4, decides. The open questions stay open until the
evidence settles them. Delivery is tracked in phase 1 of
[the roadmap](../roadmap.md).

[^1]: Commits `eba08ad` (PR #9), `37d08e5` (PR #10) and `7445bf0` (PR #12),
      as read on 2026-10-18.

[^2]: FizzBee, "Getting Started", retrieved 2026-10-18:
      <https://fizzbee.io/design/tutorials/getting-started/>

[^3]: `buildlimits/recovery_starvation.py`: `RECOVERY_STALE_DRAIN_SECONDS`,
      `RetainedDrainAge` and `proof`, at `7445bf0`.

[^4]: `buildlimits/recovery_starvation.py`: `refusal` and `blocked`;
      `CacheRecovery.request` calls `refusal`, and `CacheRecovery._try_stop`
      calls `blocked`, at `7445bf0`.

[^5]: `buildlimits/cache_recovery.py` and `buildlimits/recovery_roots.py`: at
      `37d08e5`, `_try_stop` failed a `starved` transaction with "the visible
      compiler roots proof lapsed before stopping" unless `zero_visible_roots()`
      held. At `7445bf0` it calls `recovery_roots.stop_verdict`, which returns
      `deadline` once `VisibleRootsGrace` has run for
      `RECOVERY_ROOTS_GRACE_SECONDS` (`recovery_starvation.py`).

[^6]: `buildlimits/admission.py`: `Admission.queue` is a `collections.deque`;
       `submit` appends and `token_arrived` takes `popleft`, at `7445bf0`.

[^7]: ADR 011, dev-env-rocky:
      <https://github.com/leynos/dev-env-rocky/blob/main/docs/adr-011-sccache-under-compile-admission.md>

[^8]: `buildlimits/cache_recovery.py`: `CacheRecovery.request` returns
       `describe(now) | {"blocked": reason}` without opening a transaction
       when `recovery_starvation.refusal` gives a reason, at `7445bf0`;
       `buildlimits/admission.py`: `Admission.health_failed` refuses the whole
       queue.

[^9]: FizzBee, "Current Limitations", retrieved 2026-10-18:
      <https://fizzbee.io/design/tutorials/limitations/>

[^10]: FizzBee, "Actions" (action enabling semantics), retrieved 2026-10-18:
       <https://fizzbee.io/reference/language/actions/>

[^11]: FizzBee repository, retrieved 2026-10-18:
      <https://github.com/fizzbee-io/fizzbee>

[^12]: FizzBee, "Quick Start" (testing), retrieved 2026-10-18:
      <https://fizzbee.io/testing/tutorials/quick-start/>
