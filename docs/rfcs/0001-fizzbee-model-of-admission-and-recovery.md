# RFC 0001: FizzBee model of compile admission and cache recovery

## Preamble

- **RFC number:** 0001
- **Status:** Proposed
- **Created:** 2026-10-18
- **Baseline:** `main` at `37d08e5` (PR #10), which carries the fixes for
  incidents 1, 2 and 4 (PR #9) and for incident 3 (PR #10)[^1]. One gap in
  incident 3's fix remains: the automatic stop still waits for zero visible
  roots, and is being fixed on the `stale-root-deadline` branch.
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

| Incident         | Symptom                                     | Cause                                        | Resolution                            |
| ---------------- | ------------------------------------------- | -------------------------------------------- | ------------------------------------- |
| 1. Livelock      | Retained tokens held while the stop retried | Stop guard counted uncached groups as moving | PR #9, `Counts.cached_moving`         |
| 2. Idle cooldown | New drains idle for ~15 min, not in status  | 900 s cooldown after clean endings too       | PR #9, 60 s settle and `cooling_for`  |
| 3. Capacity loss | One slot lost for 30+ min, then all slots   | Trigger needed two readings to coincide      | PR #10, stale proof and early opening |
| 4. Hidden fault  | Refused restart's fault absent from status  | Idle transaction dropped its last fault      | PR #9                                 |

_Table 1: Recovery incidents observed on 2026-10-18._

In incident 1 the transaction denies cache composition while it runs, so every
group it admits is uncached and cannot be interrupted by the stop. The stop
guard nevertheless counted those groups as moving work, so a busy queue
refilled each freed slot before every retried stop, and the retained tokens
stayed held. Incident 3 has two halves. The automatic trigger required a
live-work fault with zero visible compiler roots _and_ no cached group moving
in the same reading; under a busy queue the two never coincided, so one
retained server-drain token cost a slot for more than 30 minutes, until every
slot was retained. Meanwhile `build-limits-recover` refused outright while any
cached compile ran, so the operator could not intervene either. Incidents 2 and
4 were silent: tokens were retained correctly, but status did not say why.

PR #10 fixed both halves of incident 3. The trigger now also accepts a retained
drain held without a break for `RECOVERY_STALE_DRAIN_SECONDS = 300`
(`recovery_starvation.proof` names it `stale`, the other proof `detached`)[^7].
`recovery_starvation.refusal` refuses to open a transaction only when the user
manager transport is faulted, so `build-limits-recover` and the automatic
trigger open while cached work runs. The stop itself still waits for that work
through `recovery_starvation.blocked`[^8]. A stale-opened transaction does not
stop the cache on age alone: `CacheRecovery._try_stop` re-arms an automatic
transaction until the drain probe again proves zero visible roots[^9]. Visible
roots that never clear therefore hold the stop off, which is the gap
`stale-root-deadline` closes.

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
  and one distinguished _tracked request_ for per-request liveness;
- groups by lifecycle (`RUNNING`, `DRAINING`, `SERVER_DRAIN`), composition
  and ending, so that `Group.may_orphan_server_work` is computable;
- the last reachability reading, and whether a recovery hold is in force;
- the drain probe's fault kind, whether it proved zero visible roots, and
  the retained-drain age, which `RetainedDrainAge` measures on idle ticks and
  retires at the accounting observation, with the staleness limit as a timer
  class[^7];
- the transaction's phase, attempts (abstracted to a small limit), trigger,
  last fault and rest timer; the barrier; an operator request; health.

The environment comprises a _busy queue_ that may submit cacheable or uncached
requests whenever fewer than a bound wait, including straight after a token
returns; _builds_ whose leaders exit cleanly or by signal, and requesters who
cancel, a bounded number of times; the _managed cache_, live or stopped, with
its control group and the detached work that abnormal cached endings leave; the
_user manager_, which accepts or refuses jobs, completes them later, and may
fault and restore its transport; _timers_ for settle, cooldown, staleness and
backoff; and an _operator_.

Time is abstract. A timer is armed with a duration class and expires through an
environment action, so the checker explores every order in which timers expire
relative to other events. Where a property depends on relative durations, such
as settle being shorter than cooldown, timers count small integer units
advanced by a tick action, and the property names units.

### 2. Fairness assumptions

Liveness is checked only under the assumptions in Table 2, each stated in the
model beside the action it constrains. FizzBee documents weak (`fair` or
`fair<weak>`) and strong (`fair<strong>`) fairness on actions[^2].

| ID  | Assumption                                  | Basis                                                                 |
| --- | ------------------------------------------- | --------------------------------------------------------------------- |
| F1  | Supervisor poll steps are weakly fair       | Weak, not strong: the 1 s poll is not synchronized with group endings |
| F2  | Leaders exit and draining scopes empty      | Termination escalates to `SIGKILL` after `cancel_grace_seconds`       |
| F3  | Detached server work finishes or is stopped | A claim about sccache, not buildlimits                                |
| F4  | Accepted jobs complete; faults are finite   | A claim about the systemd user manager                                |
| F5  | Requested tokens arrive and are returned    | A claim about guildmaster and other users                             |
| F6  | Armed timers expire, in no set order        | Time is abstract; the checker orders expiries                         |
| F7  | The environment is otherwise free           | Submissions possible, never forced; cancellations bounded             |

_Table 2: Fairness assumptions under which liveness is checked._

F1 is weak because the poll may never land in a transient window, and incident
1 lived in such windows. F7 makes the busy queue an adversary that may keep the
queue non-empty for ever, while bounded cancellations keep `eventually always`
properties meaningful. F3 to F5 are claims about systemd, the kernel and
sccache rather than buildlimits; a property that needs one is only as strong as
that claim, and every counterexample report names the assumptions in force.

### 3. Properties

Safety properties hold in every reachable state or transition; liveness
properties hold under F1 to F7. Table 3 lists them.

| ID  | Property                   | Statement                                                                                         |
| --- | -------------------------- | ------------------------------------------------------------------------------------------------- |
| S1  | Stop safety                | A stop is issued only with no cached group running or draining and a readable manager             |
| S2  | Token conservation         | Tokens sum to capacity; waiting requests hold none; a token returns once, after its scope empties |
| S3  | Accounted release          | A `SERVER_DRAIN` token is released only on a reading of a stopped, empty service                  |
| S4  | Barrier ordering           | A start is issued only with the barrier absent; the barrier stands from stop to restart           |
| S5  | Composition                | No group is composed with the cache under a hold or after a negative reachability reading         |
| S6  | Server work coverage       | Live server work never exceeds the tokens held by cached groups (ADR 011[^3])                     |
| S7  | Unhealthy quiescence       | Nothing launches while the supervisor is unhealthy                                                |
| S8  | Explainable retention      | A retained token with a waiting request and no transaction always has a reason in status          |
| S9  | Fault persistence          | A refused restart's fault stays in status until a later transaction ends cleanly                  |
| L1  | Retained drains accounted  | Under the `starved` policy, eventually always no token is in `SERVER_DRAIN`                       |
| L2  | Queue progress             | The tracked request, unless cancelled, is eventually admitted                                     |
| L3  | Transactions complete      | Every opened transaction eventually returns to idle                                               |
| L4  | Operator requests complete | An operator request eventually ends in a completed transaction or accounted tokens                |
| R1  | Proportionate rest         | After a clean ending the trigger is eligible within the settle units                              |

_Table 3: Safety (S), liveness (L) and bounded-response (R) properties._

S8's reasons are a positive `cooling_for`, the drain fault with its guidance,
or the `manual` policy. L4 assumes the manager transport is not permanently
faulted. R1 captures a requirement that liveness cannot: only a faulted ending
imposes the cooldown. Table 4 maps each incident to the property it violates
and the counterexample predicted on its pre-fix behaviour.

| Incident         | Pre-fix behaviour                             | Property | Predicted counterexample                           |
| ---------------- | --------------------------------------------- | -------- | -------------------------------------------------- |
| 1. Livelock      | Stop guard counts every moving group          | L3, L1   | Lasso: stop retried as uncached groups overlap     |
| 2. Idle cooldown | Cooldown after every ending; no `cooling_for` | S8, R1   | Finite: clean ending, new drain, no reason shown   |
| 3. Capacity loss | Trigger and request rules at `eba08ad`        | L1, L4   | Lasso: one slot retained, others cycle cached work |
| 4. Hidden fault  | Idle transaction drops its last fault         | S9       | Finite: restart refused to the limit, then idle    |

_Table 4: Incidents as predicted counterexamples._

Two qualifications keep this claim honest. Incidents 1 and 3 are discovery
claims: L1, L3 and L4 say only that recovery makes progress, and nothing about
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

### 4. Illustrative sketch

For screen readers: the following block is an illustrative fragment of the
model in pseudo-FizzBee. It declares state, one supervisor action with a switch
that restores incident 1's pre-fix guard, and three properties.

```plaintext
# Illustrative pseudo-FizzBee: unchecked; syntax is to be confirmed in W1.
CAPACITY = 3
PREFIX_GUARD_COUNTS_UNCACHED = False   # True restores incident 1's guard

action Init:
    free, held, held_by_others, server_drain = CAPACITY, 0, 0, 0
    running = {"cached": 0, "uncached": 0}
    draining = {"cached": 0, "uncached": 0}
    phase, manager_faulted, stop_issued = "idle", False, False

atomic fair action RecoveryStop:       # a weakly fair poll step (F1)
    require phase == "stopping"
    moving = running["cached"] + draining["cached"]
    if PREFIX_GUARD_COUNTS_UNCACHED:
        moving += running["uncached"] + draining["uncached"]
    if moving == 0 and not manager_faulted:
        stop_issued = True
        phase = "accounting"

transition assertion StopSafety(before, after):
    if not before.stop_issued and after.stop_issued:
        return before.running["cached"] + before.draining["cached"] == 0
    return True

always assertion TokensConserved:
    return free + held + held_by_others == CAPACITY

eventually always assertion RetainedDrainsAccounted:
    return server_drain == 0
```

FizzBee's documented constructs include `action Init`, `atomic` and `fair`
actions, `require` guards, `oneof` choice, `always`, `always eventually` and
`eventually always` assertions, and `transition` assertions over `before` and
`after`[^2]. Its documentation warns that copying shared state into a local can
introduce an interleaving point, and that some function calls must stand as
separate statements[^4]; the sketch's `moving` local is exactly the pattern W1
must check.

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
   and kept as a correspondence test. None is discarded.
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
  behaviours behind incidents 1 and 3.
- **Prediction:** at capacity 3, incident 1's known-bad configuration (the
  stop guard counting uncached groups) yields a lasso to L3, and incident 3's
  (the trigger and refusal at `eba08ad`) yields lassos to L1 and L4. The fixed
  configurations (PR #9's guard, and PR #10's trigger, opening and stop) yield
  none. At capacity 2, incident 1's counterexample appears under weak but not
  strong fairness of the recovery step.
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

- **Question:** does L1 hold for the automatic stop at `37d08e5`, and for the
  fix on the `stale-root-deadline` branch, and under what assumption? At
  `37d08e5` a stale-opened transaction still needs the zero-root proof to stop,
  which a manual request skips[^9].
- **Prediction:** at `37d08e5`, L1 holds with F3 and fails without it, through
  detached work that holds a visible root for ever; L4 holds without F3. With
  the `stale-root-deadline` fix, L1 holds without F3.
- **Falsified if:** the fixed configuration fails L1 without F3, or `37d08e5`
  satisfies L1 without F3.
- **Decision:** none here. The model checks that fix; it does not design it.
  The result goes to the branch owner, who decides whether F3 is an acceptable
  operating assumption.

## Workstreams

Table 6 proposes four workstreams in dependency order; the dependencies are
acyclic.

| ID  | Outcome                                                                                | Requires          | Exit evidence                                                                | Gate                             |
| --- | -------------------------------------------------------------------------------------- | ----------------- | ---------------------------------------------------------------------------- | -------------------------------- |
| W1  | Minimal model and busy queue; known-bad and fixed configurations for incidents 1 and 3 | None              | H1 verdict, traces replayed, open questions 1 to 6 and 8 answered            | G1: proceed, change tool or stop |
| W2  | Full scope; S1 to S9, L1 to L4 and R1; known-bad configurations for all four incidents | W1 at G1          | All properties hold at `37d08e5` under F1 to F7; known-bad fail; H4 answered | G2: bounds and CI budget         |
| W3  | Drift check, counterexample replay and projection inclusion as pytest suites           | W2                | H3 verdict; every counterexample has a regression or correspondence test     | G3: required or advisory         |
| W4  | Pinned tool, `make model-check`, CI job, update policy and layout entry                | W2; W3 to require | Within H2's budget; identical results on unchanged sources                   | G4: place in the quality gates   |

_Table 6: Proposed workstreams, their evidence and the decisions they enable._

W1 excludes health, start-up, escalation detail and metrics, and is limited to
two model revisions after its first full run; it stops at H1's falsification
condition. At `eba08ad`, W2 expects L1 and L4 to fail, because that revision is
incident 3's pre-fix behaviour; at `37d08e5` it expects them to hold under F3.
W3 may add journal trace checking as an optional tool.

## Risks and mitigations

| Risk                     | Mitigation                                                                 |
| ------------------------ | -------------------------------------------------------------------------- |
| State explosion          | Counts by class; minimal bounds that reproduce Table 4; split models (H2)  |
| Model drift              | Drift check, update policy, known-bad configurations, projection inclusion |
| False confidence         | Every report names its fairness assumptions and conformance limits         |
| Generous fairness        | F1 is weak, not strong; F2 to F5 reviewed against systemd and sccache      |
| Spurious counterexamples | Replay before triage; one correction round per trace                       |
| Tool immaturity          | Pinned version; workarounds recorded in W1[^4]; TLA+ fallback              |
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

1. FizzBee's syntax for records and collections in state, and whether
   fairness can apply per instance or parameter (each group, each timer) rather
   than per action, for example through roles.
2. The liveness-checking modes and how lasso counterexamples are reported;
   documented checker output shows a `liveness: "strict"` option whose meaning
   is unconfirmed[^2].
3. Whether traces and state graphs can be exported in a machine-readable
   form for replay and projection inclusion.
4. Packaging of the Linux release: runtime dependencies, checksums or
   signatures, and pinning. Prebuilt Linux binaries are documented[^2].
5. The licence of the pinned release. The repository page listed Apache-2.0
   when read[^5]; confirm it for the pinned release before adoption.
6. Performance at H2's bounds, and whether symmetry or partial-order
   reduction is available.
7. Whether FizzBee's model-based testing tool, whose quick start targets
   Go[^6], can drive Python; if not, W3's replay stays bespoke.
8. The exact pre-fix revisions for incidents 1, 2 and 4, which predate the
   import into this repository. Known-bad configurations must transcribe those
   revisions, not a recollection of them.
9. Whether to add `cached_moving` and the hold to the metric gauges, a code
   change for a separate decision that would let journal trace checking see the
   variables behind incidents 1 and 3.
10. Whether `make model-check` joins `make all`, and whether the CI job
    becomes required after G3.
11. Whether the supervisor journal records from 2026-10-18 are retained, so
    that the incidents can serve as trace-checking fixtures.
12. Whether supervisor crash and restart, with start-up accounting and
    barrier reclamation, belong in W2 or a later revision.

## Recommendation

Approve W1 now and decide the rest at G1. The incidents expose a class of
defect, progress under a busy and adversarial environment, that the current
verification cannot express, and FizzBee offers liveness checking under
explicit fairness in a notation close to the code. The spike tests that claim
cheaply and decisively. If FizzBee cannot rediscover incidents 1 and 3 on their
pre-fix behaviour, the RFC should be withdrawn or redirected to TLA+ or to
extending Hypothesis, and that result recorded.

[^1]: Commits `eba08ad` (PR #9) and `37d08e5` (PR #10), as read on
      2026-10-18.

[^2]: FizzBee, "Getting Started", retrieved 2026-10-18:
      <https://fizzbee.io/design/tutorials/getting-started/>

[^3]: ADR 011, dev-env-rocky:
      <https://github.com/leynos/dev-env-rocky/blob/main/docs/adr-011-sccache-under-compile-admission.md>

[^4]: FizzBee, "Current Limitations", retrieved 2026-10-18:
      <https://fizzbee.io/design/tutorials/limitations/>

[^5]: FizzBee repository, retrieved 2026-10-18:
      <https://github.com/fizzbee-io/fizzbee>

[^6]: FizzBee, "Quick Start" (testing), retrieved 2026-10-18:
      <https://fizzbee.io/testing/tutorials/quick-start/>

[^7]: `buildlimits/recovery_starvation.py`: `RECOVERY_STALE_DRAIN_SECONDS`,
      `RetainedDrainAge` and `proof`, at `37d08e5`.

[^8]: `buildlimits/recovery_starvation.py`: `refusal` and `blocked`;
      `CacheRecovery.request` calls `refusal`, and `CacheRecovery._try_stop`
      calls `blocked`, at `37d08e5`.

[^9]: `buildlimits/cache_recovery.py`: `_try_stop` fails a `starved`
      transaction with "the visible compiler roots proof lapsed before
      stopping" unless `zero_visible_roots()` holds, at `37d08e5`.
