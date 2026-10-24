# Buildlimits roadmap

This roadmap sequences work accepted in
[RFC 0001](rfcs/0001-fizzbee-model-of-admission-and-recovery.md) on 2026-10-18
and [RFC 0002](rfcs/0002-cache-hit-prediction.md) on 2026-10-23. It does not
promise dates. Each phase carries one testable idea, the steps underneath it
work to validate or falsify that idea, and each task is a review-sized unit of
delivery. Tests, model runs and proofs are part of the task that needs them,
and appear as a `Success:` criterion where the outcome is not obvious.

Section references of the form "RFC 0001 §n" or "RFC 0002 §n" name numbered
subsections of the relevant RFC's proposed design. Hypothesis, workstream, gate
and open-question references belong to the RFC named in the phase; use an
explicit qualifier such as "RFC 0002 G1" where both RFCs use the same
identifier. RFC 0001's phase remains model-only and does not change runtime
behaviour or interfaces. Phase 3 is limited to RFC 0002 W1 and its gated W2: it
adds no prediction display, hit-first admission, shadow mode or direct
composition. The developer's guide remains the source of truth for the code the
model transcribes.

## 1. Model admission and recovery under explicit fairness

Idea: if a FizzBee model of the token pool, admission, the managed cache, the
drain probe and the recovery transaction can rediscover incidents 1 and 3 on
their pre-fix behaviour, and then hold on `main` under stated fairness, then
progress against a busy queue becomes checkable. That is a question the
Hypothesis state machines and CrossHair contracts cannot ask. If the model
cannot do this, the cost stops at the first gate and the result is recorded.

The phase follows the RFC's workstreams in order. Step 1.1 is a spike gated by
G1. Steps 1.2 and 1.3 build and apply the full model (W2, gated by G2). Step
1.4 keeps the model honest against the code (W3, gated by G3), and step 1.5
puts it in the quality gates (W4, gated by G4). Stopping at any gate leaves the
repository no worse than today, with the evidence recorded.

### 1.1. Rediscover incidents 1 and 3 on a minimal model

This step answers whether FizzBee, under the abstraction of RFC 0001 §1 and the
fairness of §2, reproduces the two liveness incidents on their pre-fix
behaviour and passes on the fixed behaviour. Its outcome is the H1 verdict,
which decides at G1 whether to proceed, change tool or stop. See RFC 0001 §§1-3
and H1.

- [x] 1.1.1. Identify the pre-fix revisions of incidents 1, 2 and 4.
  - See RFC 0001 open question 8.
  - Search the dev-env-rocky history that predates the import; incident 3's
    pre-fix revision is `eba08ad` in this repository.
  - Success: each revision is named by commit, or recorded as unrecoverable
    with the transcription basis stated, in the RFC's open-questions record.
- [ ] 1.1.2. Settle FizzBee's language and checker behaviour with a toy model.
  - See RFC 0001 open questions 1 to 3 and 6.
  - Establish state records and collections, per-instance fairness, the
    liveness modes (including the meaning of `liveness: "strict"`), how lasso
    counterexamples are reported, trace and state-graph export, and whether
    symmetry or partial-order reduction exists.
  - Record workarounds, including the interleaving points in the RFC's sketch.
  - Success: each question is answered with a runnable example, or marked
    unanswerable with the consequence for W3.
- [x] 1.1.3. Settle FizzBee's packaging and licence for a pinned release.
  - See RFC 0001 open questions 4 and 5.
  - Record the Linux release's runtime dependencies, checksum or signature, and
    the licence of the candidate release.
  - Success: a candidate version, checksum and licence are recorded, or the
    absence of any is recorded as a G1 input.
- [ ] 1.1.4. Write the minimal model with the busy queue under
      `models/admission/`.
  - Requires 1.1.2.
  - Cover the pool, cacheable and uncached requests, one tracked request,
    groups by lifecycle and composition, the cache hold, the drain probe, the
    retained-drain age, and the recovery phases. Exclude health, start-up,
    escalation detail and metrics, as RFC 0001 W1 requires.
  - State each fairness assumption beside the action it constrains.
  - Success: S1, S2, L1, L3 and L4 check at capacity 3 with every report
    naming the assumptions in force.
- [ ] 1.1.5. Add incident 1 and incident 3 as known-bad and fixed
      configurations.
  - Requires 1.1.1 and 1.1.4.
  - Provide the `PREFIX_GUARD_COUNTS_UNCACHED` switch and the trigger and
    refusal rules at `eba08ad`, beside the fixed configurations of PRs #9, #10
    and #12 (`7445bf0`).
  - Success: at capacity 3, incident 1 yields a lasso to L3 and incident 3
    yields a lasso to L1 and a finite counterexample to L4, and the fixed
    configurations yield none; at capacity 2, incident 1's lasso appears under
    weak but not strong fairness. A result that inverts either prediction is
    reported, not tuned away.
- [ ] 1.1.6. Replay each counterexample through the production doubles.
  - Requires 1.1.5.
  - Drive `Admission`, `ServerWatch` and `CacheRecovery` with `FakePool` and
    `cache_recovery_harness`, as RFC 0001 §5 prescribes.
  - Success: every trace has a disposition. A trace the code follows becomes a
    scripted test that fails before its fix and passes after; a trace it cannot
    follow becomes a corrected model and a correspondence test. None is dropped.
- [ ] 1.1.7. Measure the Hypothesis comparator for incident 1.
  - Requires 1.1.5.
  - Extend the recovery machine with uncached groups and a bounded-progress
    proxy, the stop issued within N steps, with N justified.
  - Success: the comparator's cost to find incident 1 is recorded beside the
    model's, so G1 can prefer extending Hypothesis if it is comparable.
- [ ] 1.1.8. Record the H1 verdict and the G1 decision in RFC 0001.
  - Requires 1.1.1 to 1.1.7.
  - Allow at most two model revisions after the first full run.
  - Success: the RFC states proceed, change tool (repeat the spike once in
    TLA+) or stop, with the evidence; a negative result is recorded as such.

### 1.2. Model the coupling of admission and recovery in full

This step answers whether the full model checks every property on `main` within
bounds a pull request can afford. It requires a G1 decision to proceed, and its
outcome is the H2 verdict and the bounds that G2 fixes. See RFC 0001 §§1-3, H2
and W2.

- [ ] 1.2.1. Add the remaining environment actions to the model.
  - Requires 1.1.8.
  - Add builds that exit cleanly or by signal, bounded cancellations, the
    managed cache with detached work, the user manager with transport faults,
    the operator, health, and the restart barrier.
  - Success: S1, S2, S4, S5 and S7 hold at capacity 3 with two waiting
    requests.
- [ ] 1.2.2. Add the remaining safety properties S3, S6, S8 and S9.
  - Requires 1.2.1.
  - Express stop safety, accounted release, server work coverage, explainable
    retention and fault persistence as `always` and `transition` assertions.
  - Success: all of S1 to S9 hold on the `7445bf0` configuration.
- [ ] 1.2.3. Add the liveness properties L1 to L4 under F1 to F7.
  - Requires 1.2.1.
  - Success: L1 to L4 are checked with each fairness assumption stated beside
    its action and named in every report.
- [ ] 1.2.4. Add abstract timers, property R1 and the incident 2 configuration.
  - Requires 1.2.2.
  - Count settle, cooldown and staleness in small integer units advanced by a
    tick action, so the checker explores every expiry order.
  - Success: the pre-fix cooldown yields a finite counterexample to S8 and R1,
    and the fixed configuration passes both.
- [ ] 1.2.5. Add the incident 4 configuration and check RFC 0001's Table 4 as a
      whole.
  - Requires 1.2.3 and 1.2.4.
  - Success: each of the four known-bad configurations fails its Table 4
    property, and every property holds on the `7445bf0` configuration under F1
    to F7.
- [ ] 1.2.6. Measure runtime at H2's bounds and record the G2 decision.
  - Requires 1.2.5.
  - Use capacity 3 with two waiting requests, two abnormal endings, one manager
    fault and a restart limit of 2.
  - Success: the smallest bounds that reproduce every incident are timed
    against the 10-minute pull-request and 60-minute nightly budgets. The RFC
    records whether the model stays whole, runs nightly only, or is split into
    a coupling model and a transaction model.

### 1.3. Check the stale-path stop and the restart design

This step answers whether the model confirms the fix to the stale-path stop,
merged since as pull request 12, and whether start-up quiescence, being
designed now, belongs in the model. It checks both and plans neither. It
informs, through F3, whether an operating assumption is acceptable. See RFC
0001 §2 and H4.

- [ ] 1.3.1. Check the stale-path automatic stop at `37d08e5` as a known-bad
      configuration.
  - Requires 1.2.3.
  - At `37d08e5` a stale-opened transaction stopped only after the drain probe
    again proved zero visible roots (`CacheRecovery._try_stop`); a manual
    request skipped the proof.
  - Success: the H4 prediction is confirmed or falsified: L1 holds with F3 and
    fails without it, through detached work holding a visible root for ever,
    and L4 holds without F3. A confirmed result is kept as a fifth known-bad
    configuration, and any counterexample is replayed as in 1.1.6.
- [ ] 1.3.2. Check the visible-roots grace of PR #12 at `7445bf0`.
  - Requires 1.3.1.
  - Model `recovery_roots.stop_verdict` with `RECOVERY_ROOTS_GRACE_SECONDS`
    counted in timer units, including that an unreadable root count neither
    starts nor completes the clock and that a blocked stop breaks it.
  - Success: L1 holds at `7445bf0` without F3 when F4 includes a completed stop
    job leaving an empty control group (H4), or the counterexample is replayed
    through the production doubles and becomes a regression test. The
    model designs nothing in the fix.
- [ ] 1.3.3. Decide whether start-up quiescence belongs in the model.
  - Requires 1.2.6.
  - See RFC 0001 open question 12. The non-disruptive supervisor restart being
    designed on branch `graceful-restart` stops a live managed cache at
    start-up, under the restart barrier, before accounting and barrier
    reclamation.
  - Success: the RFC records the decision. If it is in scope, a start-up
    action cites the merged code, and S3, S4 and L1 hold across a supervisor
    restart that finds a live cache; if not, the deferral is recorded in 2.1.
- [ ] 1.3.4. Close W2 by recording that its exit evidence is met.
  - Requires 1.2.6, 1.3.2 and 1.3.3.
  - Success: RFC 0001 W2 shows all properties holding at `7445bf0` under F1 to
    F7, the known-bad configurations failing, and H4 answered.

### 1.4. Keep the model honest against the code

This step answers whether drift can be detected mechanically and whether the
model permits everything the code does. Its outcome is the H3 verdict, which
decides at G3 whether the check is required or advisory. See RFC 0001 §5 and H3.

- [ ] 1.4.1. Add the mechanical drift check.
  - Requires 1.2.5.
  - Each supervisor action carries a `# maps: <dotted path>` comment. Parse the
    model and fail on an unmapped public `Admission` event method, `Effect`
    subclass, `Phase` literal or fault kind, and on a mapped path that no longer
    resolves.
  - Success: renaming a mapped function, or adding an unmapped `Effect`, fails
    the check.
- [ ] 1.4.2. Turn every counterexample into a permanent regression test.
  - Requires 1.1.6 and 1.2.5.
  - Replay all traces from the full model, and keep each known-bad trace as a
    test of the revision it transcribes.
  - Settle whether FizzBee's model-based testing can drive Python (open
    question 7); if not, keep the replay bespoke.
  - Success: every counterexample has a regression or correspondence test in
    `tests/`.
- [ ] 1.4.3. Add projection inclusion from the Hypothesis machines.
  - Requires 1.2.5 and 1.4.1.
  - Record each production transition projected onto the model's state, and
    require the model to permit it. Use state-graph export if 1.1.2 found it,
    and a bespoke enumeration otherwise.
  - Success: no divergence remains after one correction round to either side.
- [ ] 1.4.4. Record the H3 verdict and the G3 decision.
  - Requires 1.4.2 and 1.4.3.
  - Success: RFC 0001 states whether `make model-check` may become required
    (H3 survived) or stays advisory with the gap recorded.

### 1.5. Run the model check as a quality gate

This step answers whether the model check runs deterministically within budget
on a hosted runner. Its outcome is G4, which places the check in the
repository's quality gates. See RFC 0001 §6, H2 and W4.

- [ ] 1.5.1. Pin the tool and add `make model-check`.
  - Requires 1.1.3 and 1.2.6.
  - Pin `FIZZBEE_VERSION` and a release checksum, install under the local-only
    `.tools/`, and check the routine bounds and every known-bad configuration.
  - Success: two runs on unchanged sources give identical results, and the
    target fails if a known-bad configuration stops producing its
    counterexample.
- [ ] 1.5.2. Add the pull-request and nightly CI jobs.
  - Requires 1.5.1.
  - Run on pull requests that touch `models/` or the update-policy modules
    (`admission`, `admission_state`, `effects`, `groups`, `group_drain`,
    `server_watch`, `sccache`, `cache_recovery`, `recovery_starvation`,
    `recovery_barrier`, `supervisor_polling`), and nightly with larger bounds.
  - Success: the jobs finish within the budgets 1.2.6 settled.
- [ ] 1.5.3. Document the model, its update policy and the layout entry.
  - Requires 1.4.4 and 1.5.2.
  - Add `models/admission/` to the repository layout, a developer's guide
    section on the model and the update policy, and the contents entry.
  - Record the G4 decision on whether `make model-check` joins `make all` and
    whether the CI job becomes required (open question 10).
  - Success: `make markdownlint` and `make nixie` pass, and the developer's
    guide states when a change must update the model or say why it need not.

## 2. Deferred extensions after the model check

Idea: if the model check is already trustworthy and boring to run, the project
can weigh its extensions on their value rather than letting them destabilize
the first adoption.

### 2.1. Evaluate what the RFC leaves open

This step answers which extensions are worth a separate decision once phase 1
has produced evidence. See RFC 0001 open questions 9, 11 and 12.

- [ ] 2.1.1. Decide whether to add `cached_moving` and the hold to the metric
  gauges.
  - Requires phase 1.
  - See RFC 0001 open question 9. This is a code change, so it needs its own
    decision; it would let journal records show the variables behind incidents
    1 and 3.
  - Success: a decision is recorded with its cost to the journal schema.
- [ ] 2.1.2. Add journal trace checking as an optional tool.
  - Requires 1.4.4 and 2.1.1.
  - See RFC 0001 §5 and open question 11. Confirm whether the supervisor
    journal records from 2026-10-18 are retained to serve as fixtures.
  - Success: a checker confirms a recorded sequence is a model path up to
    stuttering, and reports "no contradiction found", never refinement.
- [ ] 2.1.3. Model supervisor crash and restart, if 1.3.3 deferred it.
  - Requires 1.3.3.
  - See RFC 0001 open question 12.
  - Success: start-up accounting and barrier reclamation are modelled with the
    properties of 1.3.3, or the RFC records why they are not.

## 3. Record and evaluate cache outcomes

Idea: if buildlimits can attribute real sccache outcomes at quiet points and
measure H2 child sightings where H1 solo-window coverage falls below its 5%
floor, the resulting local records can support an offline evaluation of
prediction without changing admission. See RFC 0002 §§1-2 and
[ADR 005](adr-005-cache-outcome-recording.md).

### 3.1. Record outcomes and decide G1

This step delivers RFC 0002 W1, including the real-binary feasibility check,
bounded local recording, the H3 replay and H2 child sightings. Its outcome is
the H1-H3 evidence and the owner's RFC 0002 G1 decision.

- [ ] 3.1.1. Complete EP-M1 to EP-M8 of the cache-hit prediction ExecPlan.
  - Requires the RFC 0002 W1 acceptance recorded in [RFC
    0002](rfcs/0002-cache-hit-prediction.md).
  - Fix the thresholds and H2 classifier in ADR 005 before the live statistics
    fixture or outcome records are read.
  - Success: the read-only live fixture is redacted; labels, H3 evidence and
    child sightings are bounded and local; the replay reports H2 and H3
    against the fixed thresholds, while the scripted and controlled windows
    supply the H1 verdict; G0 and all material coverage limits are stated.

### 3.2. Evaluate prediction offline after G1

This step is RFC 0002 W2 and requires an explicit RFC 0002 G1 decision to
proceed. It runs only against stored records and evaluates H4 and H5; its
outcome informs the separate RFC 0002 G2 decision.

- [ ] 3.2.1. Complete EP-M9 of the cache-hit prediction ExecPlan.
  - Requires RFC 0002 G1 to release W2.
  - See RFC 0002 §2 and H4-H5. Do not run this step on the live cache.
  - Success: the replay reports the registry base-rate comparison, digest
    stability and each verdict against ADR 005's fixed minimums and
    thresholds; evidence and limitations are recorded for G2.
