# Model compile admission and cache recovery in FizzBee

This ExecPlan (execution plan) is a living document. The sections
`Constraints`, `Tolerances`, `Risks`, `Progress`, `Surprises & discoveries`,
`Decision log`, `Outcomes & retrospective`, `Conformance basis` and
`Verification plan` must be kept up to date as work proceeds.

Status: DRAFT

Approval: not yet approved. No implementation may begin until the user
explicitly approves this plan. Approval of the plan authorizes milestones
EP-M1 to EP-M5 only (RFC 0001 workstream W1). Each later gate, G1 to G4,
requires a separate explicit decision recorded in `Decision log`, as RFC 0001's
acceptance requires.

## Purpose / big picture

Four defects in compile admission's cache recovery reached the shared
development host on 2026-10-18 before any test found them. Each was a defect of
behaviour over time: a recovery that never stopped the cache while a busy queue
refilled each freed slot, a rest that idled retained tokens without
explanation, a trigger whose evidence never coincided under load, and a fault
that vanished from status. The existing Hypothesis state machines and CrossHair
contracts check safety over short random interleavings. None checks progress
(liveness: "something good eventually happens"), none models a busy queue, and
none composes admission with recovery.

[RFC 0001](../rfcs/0001-fizzbee-model-of-admission-and-recovery.md), accepted
on 2026-10-18, proposes a FizzBee model of the token pool, admission, the
managed cache, the drain probe and the four-phase recovery transaction. FizzBee
is an open-source model checker whose specification language is close to
Python; it explores every reachable state of a model within stated bounds and
checks safety and liveness properties under declared fairness (an assumption
that an action which stays enabled is eventually taken).

After this plan is delivered, a maintainer can run one command:

```bash
make model-check
```

and see, for every model configuration, a line stating whether each property
holds or fails with the expected counterexample, together with the fairness
assumptions in force. The command fails if a fixed configuration violates a
property, if a deliberately broken ("known-bad") configuration that transcribes
a historical incident stops producing its counterexample, or if the checker's
output cannot be interpreted. A companion pytest suite fails when the code
gains an admission event, effect, recovery phase or fault kind that the model
does not map, and replays every counterexample through the production
`Admission`, `ServerWatch` and `CacheRecovery` objects.

Nothing changes for users of the installed package. The wheel, runtime
behaviour, thresholds and interfaces are untouched (RFC 0001 non-goals).

## Constraints

- No runtime behaviour, threshold, configuration key, command-line interface,
  status document, metric or journal field in `buildlimits/` may change. The
  model checks the design; it designs nothing (RFC 0001 non-goals, H4
  "Decision: none here"). A genuine counterexample against HEAD is reported to
  the user as a defect with a replayed failing test; fixing it is a separate,
  separately approved change.
- The wheel's contents must not change. `pyproject.toml`
  `[tool.hatch.build.targets.wheel] packages = ["buildlimits"]` stays as it is,
  and nothing under `models/` or `modelcheck/` is packaged.
- No new Python runtime or development dependency is added without escalation.
  The tooling uses the standard library (`subprocess`, `tomllib`, `json`,
  `pathlib`, `importlib`, `typing`) plus packages already in the `dev` group
  (`cyclopts`, `pytest`, `pytest-bdd`, `hypothesis`, `syrupy`).
- The FizzBee binary is a local, pinned, checksum-verified tool installed under
  the git-ignored `.tools/` directory. It is never committed, never installed
  system-wide, and never fetched by `make test`, `make lint`, `make typecheck`
  or `make check-fmt`.
- Every known-bad configuration must transcribe a specific revision (a commit
  in this repository or in `leynos/dev-env-rocky`), not a recollection of it
  (RFC 0001 open question 8). Where a revision is unrecoverable, the
  configuration records that fact and its transcription basis.
- Known-bad results that invert a prediction are reported, never tuned away.
  W1 is limited to two model revisions after its first full run (RFC 0001 W1).
- Domain and policy logic in `modelcheck/` must not import from its adapters,
  perform input or output, spawn processes or read the clock. Adapters
  implement ports that the domain declares (hexagonal architecture; see
  `Interfaces and dependencies`).
- Every Python module stays under 400 lines (`AGENTS.md`). Every public
  function carries a docstring with an example; doctests run under
  `make test` because pytest uses `--doctest-modules`.
- Tests live under `tests/` only (`AGENTS.md`).
- Gates run sequentially, never overlapped, and full gate runs are delegated to
  the `scrutineer` agent (user instructions).
- `typos.toml` is generated and never edited by hand; spelling exceptions go in
  `typos.local.toml` as exact or full-line patterns.

## Tolerances (exception triggers)

- Gates: EP-M5 ends at G1, EP-M8 at G2, EP-M10 at G3 and EP-M12 at G4. At each
  gate, stop, record the evidence and the proposed decision in RFC 0001 and in
  `Decision log`, and wait for the user's explicit decision. Do not start the
  next workstream on a recommendation alone.
- Model revisions: if W1 needs a third model revision after its first full
  run, stop. This is H1's falsification boundary.
- Prediction inversion: if any known-bad configuration fails to produce its
  predicted counterexample, or a fixed configuration fails, after the one
  permitted correction round, stop and report with the trace and replay
  result.
- Runtime budget: if the routine (pull-request) bounds exceed 10 minutes on the
  development host, or the nightly bounds exceed 60 minutes, stop and present
  H2's options (whole, nightly only, split).
- Scope: if any milestone touches more than 25 files or adds more than 1,500
  net lines of Python (excluding `.fizz` models, fixtures and snapshots), stop
  and escalate.
- Production code: if any change to a file under `buildlimits/` appears
  necessary, stop. This includes adding a metric gauge (RFC 0001 open question
  9, deferred to roadmap 2.1.1).
- Dependencies: if `cuprum` or any other new package is needed, stop and
  escalate (see `Decision log` entry D5).
- Tool behaviour: if the pinned FizzBee release cannot express a property or a
  fairness assumption the RFC requires, and no recorded workaround exists, stop
  and report at G1 with the TLA+ fallback.
- Iterations: if a gate still fails after three fix attempts, stop and
  escalate with the failing log path.
- Ambiguity: if the pre-fix revision of an incident admits two materially
  different transcriptions, stop and present both.

## Risks

- Risk: the "fixed" baseline in RFC 0001 is `7445bf0`, but HEAD (`42f40e4`) has
  changed `admission.py`, `admission_state.py`, `cache_recovery.py`,
  `recovery_starvation.py`, `server_watch.py` and added `recovery_stop.py`
  (PRs #14, #15 and #21). Severity: medium. Likelihood: certain. Mitigation:
  the fixed configuration transcribes HEAD at the start of EP-M3, and EP-M3
  records a diff audit showing which changes are refactors (no model effect)
  and which are behaviour. The known behaviour change is graceful closing
  (`Admission.begin_closing`, the `closing` gate and
  `supervisor_shutdown.settle_graceful_stop`'s manual recovery request), which
  W1 excludes with start-up and shutdown. The drift check lists those methods
  as explicit, reasoned exclusions. See `Decision log` D1.
- Risk: the Linux release is a 335 MB tarball (a Go checker, a bundled Python
  parser and a bash wrapper). Severity: medium. Likelihood: certain.
  Mitigation: install once under `.tools/fizzbee-<version>/`, verify the
  SHA-256 digest, and cache it in CI keyed on version and digest.
- Risk: `fizz` exits 0 when a check fails; failure is visible only on standard
  output. Severity: high (a silent false pass). Likelihood: certain on HEAD of
  FizzBee; to be confirmed for v0.5.4. Mitigation: the output parser fails
  closed: a run counts as a pass only with exit status 0 and a
  `PASSED: Model checker completed successfully` line and no `FAILED`,
  `DEADLOCK` or `Model checker stopped` line. Property tests in EP-M2 check
  that no text without the pass line is ever classified as a pass.
- Risk: FizzBee's documentation says liveness checking is off by default; its
  `main.go` treats an unset value as strict. Severity: high (vacuous liveness).
  Likelihood: medium. Mitigation: every configuration sets `liveness` explicitly
  in the materialized frontmatter, and the e2e suite includes a negative
  control (a toy model whose liveness property must fail).
- Risk: identifier-free counts give action-level, not per-group, fairness.
  Weak fairness on "some leader exits" does not force a particular group to
  exit. Severity: medium. Likelihood: medium. Mitigation: groups are
  interchangeable in the properties except the tracked request, which is a
  distinguished variable; the toy model in EP-M1 settles whether FizzBee roles
  give per-instance fairness (they do, per its liveness tutorial) and whether
  that is needed for L2.
- Risk: FizzBee does not support assertions inside roles, `import`, `del` or
  tuple unpacking in `for`, and copying state into a local introduces an
  interleaving point. Severity: medium. Likelihood: certain. Mitigation:
  properties stay top-level; guards use `require` over state read in place;
  EP-M1 records each workaround with a runnable example.
- Risk: the full model may exceed 400 lines, and FizzBee has no `import`.
  Severity: low. Likelihood: medium. Mitigation: the materializer assembles an
  ordered list of fragments into one specification in the output directory
  (see `Decision log` D4); the W1 model is expected to fit in one file.
- Risk: the pre-fix revisions of incidents 1, 2 and 4 predate the import and
  live in `leynos/dev-env-rocky`. Severity: medium. Likelihood: medium.
  Mitigation: EP-M1 searches that history with `gh` and `git log -S`; an
  unrecoverable revision is recorded with its transcription basis.
- Risk: state explosion at H2's bounds. Severity: medium. Likelihood: medium.
  Mitigation: counts by class, symmetry where groups are held in a `bag()`,
  minimal bounds that still reproduce Table 4, and H2's split option.
- Risk: `ty` or pytest may not resolve the new top-level `modelcheck` package.
  Severity: low. Likelihood: low. Mitigation: EP-M2 adds `"."` to pytest's
  `pythonpath` and confirms `make typecheck` resolves the package before any
  logic is written.

## Progress

- [x] (2026-10-23 23:40Z) Renamed the branch to
  `fizzbee-model-of-admission-and-recovery`.
- [x] (2026-10-24 00:05Z) Reconnaissance by a Wyvern team: production code
  map, test doubles and harnesses, documentation conventions; FizzBee research
  through Firecrawl.
- [x] (2026-10-24 00:30Z) First draft of this ExecPlan.
- [ ] Community-of-experts review of the draft and revision.
- [ ] Draft pull request opened for review.
- [ ] User approval of the plan.
- [ ] EP-M1: pre-fix revisions, pinned FizzBee and toy-model answers.
- [ ] EP-M2: `modelcheck` domain, ports, adapters and CLI, with tests.
- [ ] EP-M3: minimal model with the busy queue (W1).
- [ ] EP-M4: incident 1 and 3 known-bad configurations, replay and
  comparator.
- [ ] EP-M5: H1 verdict recorded; stop at G1.

## Surprises & discoveries

- Observation: `fizz` exits 0 when an assertion, deadlock or liveness check
  fails. Evidence: FizzBee `main.go` returns nil on failure branches, and the
  `fizz` wrapper comments that the binary "doesn't exit non-zero on FAILED"
  (read 2026-10-24 at FizzBee HEAD `ae9d9cf`). Impact: the runner must parse
  standard output and fail closed; see the Risks entry and the EP-M2 property
  tests.
- Observation: FizzBee's model-based testing supports Go, Java, Rust and
  TypeScript, not Python (RFC 0001 open question 7). Evidence:
  `mbt/lib` in the FizzBee repository and its "work in progress" page. Impact:
  counterexample replay stays bespoke (EP-M4, EP-M9).
- Observation: on a failure FizzBee writes `error-graph.json` (the failing path
  as JSON) to the output directory, and on a pass writes node and link JSON
  files. Evidence: FizzBee `main.go` and documentation. Impact: traces and
  state graphs are machine-readable (open question 3, provisionally); EP-M1
  confirms the exact schema for v0.5.4.
- Observation: main has moved past the RFC's baseline in the modelled modules.
  Evidence: `git diff --stat 7445bf0 HEAD -- buildlimits/` lists six of them.
  Impact: `Decision log` D1.
- Observation: the repository has no `docs/*-design.md` technical design
  document. Evidence: `docs/` listing and the documentation style guide's
  expectation of one. Impact: design decisions are recorded in RFC 0001 (the
  accepted design), in ADR 004 for the tooling architecture, and in the
  developer's guide for internal interfaces; see D7.

## Decision log

- D1. Decision: the "fixed" configuration transcribes HEAD of `main` at the
  start of EP-M3 (currently `42f40e4`), not `7445bf0`. EP-M3 audits
  `git diff 7445bf0 <head> -- buildlimits/` and records in RFC 0001 that the
  recovery dynamics are unchanged (refactors into `recovery_outlook.py` and
  `recovery_stop.py`) and that admission gained graceful closing, which W1
  excludes. Rationale: replay drives the production code at HEAD, so the
  model must transcribe HEAD for replay to be meaningful; the RFC's predictions
  about `7445bf0` carry over because the recovery transitions are identical.
  This is a mechanical update of a baseline, not an architecture deviation; if
  the audit finds a behavioural change inside W1's scope, it becomes a
  deviation and the plan is set to `BLOCKED`. Date/Author: 2026-10-24, planning
  agent.
- D2. Decision: the tooling is a top-level Python package `modelcheck/`, not
  shipped in the wheel, organized as a hexagon: pure domain modules, a
  `ports` module of `typing.Protocol` interfaces, and adapters for the FizzBee
  command line, the `buildlimits` code inventory and the command-line entry
  point. The models live in `models/admission/`. Rationale: the tooling has
  real policy (expectation judgement, materialization of variants, drift
  computation, trace normalization) that must be unit-tested without the
  335 MB binary; ports let the BDD scenarios run against a fake checker.
  `tests/` holds only tests and test doubles, as `AGENTS.md` and the
  repository layout require. Recorded as ADR 004. Date/Author: 2026-10-24,
  planning agent.
- D3. Decision: each expectation is checked in its own run, with every other
  assertion removed from the materialized specification. Rationale: FizzBee
  reports the first failing property, so a known-bad configuration could fail a
  different property first and either mask or impersonate the predicted one.
  One property per run makes attribution exact and lets the report state which
  fairness assumptions that property relied on. Date/Author: 2026-10-24,
  planning agent.
- D4. Decision: variants are produced by a pure materializer from one source
  specification. It rewrites only top-level constant lines named in the
  configuration (for example `PREFIX_GUARD_COUNTS_UNCACHED = True`), the
  fairness keyword of actions tagged `# fairness: <id>`, the YAML frontmatter
  (bounds and `liveness`), and the set of retained assertions. If a model is
  ever split, the materializer concatenates an ordered fragment list first.
  Rationale: FizzBee has no `import` or parameters, and hand-copied variant
  files would drift from the fixed model, which is exactly the false
  confidence RFC 0001 §5 warns against. Date/Author: 2026-10-24, planning
  agent.
- D5. Decision: the FizzBee adapter uses the standard library's `subprocess`
  with an argument list, an explicit timeout and no shell, as
  `tests/test_crosshair.py` does, rather than `cuprum`. Rationale: `cuprum` is
  not a dependency; the scripting standards apply to `scripts/`, and adding a
  dependency requires escalation. If review requires `cuprum`, that is a
  dependency decision for the user. Date/Author: 2026-10-24, planning agent.
- D6. Decision: tests that need the real FizzBee binary carry a new pytest
  marker `fizzbee` and skip, with a reason naming `make fizzbee`, when the
  pinned binary is absent. `make model-check` installs the binary and runs
  them; CI runs `make model-check` in its own job (EP-M11). Rationale:
  `make test` must stay hermetic and fast; the model check is its own gate,
  as RFC 0001 §6 proposes. Whether `make model-check` joins `make all` is G4's
  decision (open question 10). Date/Author: 2026-10-24, planning agent.
- D7. Decision: with no technical design document in the repository, design
  decisions are recorded in RFC 0001 (answers to open questions, H1 to H4
  verdicts and gate decisions), in ADR 004 (tooling architecture), and in a new
  developer's guide section 11.6 (internal interfaces and the update policy).
  `docs/users-guide.md` is not changed: the change has no user-visible
  behaviour, and the style guide excludes maintainer-only tooling from it.
  Date/Author: 2026-10-24, planning agent.
- D8. Decision: install the FizzBee binary from EP-M1 through a pinned
  `make fizzbee` target, rather than waiting for roadmap task 1.5.1. Rationale:
  every W1 task needs a reproducible binary; 1.5.1 then adds the routine
  bounds, the known-bad sweep and CI on top of an existing pin. Date/Author:
  2026-10-24, planning agent.

## Outcomes & retrospective

Not started. To be completed at each gate and at completion.

## Context and orientation

The repository is a Python 3.14 package, `buildlimits/`, that runs a
supervisor which admits Rust compiler invocations into a shared token pool so
only a few compile at once. The terms below are used throughout.

A _token_ is one compile slot from the shared pool (`buildlimits/pool.py`;
`FakePool` in `tests/pool_helpers.py` is the test double). A _request_ is a
compile waiting for a token. A _group_ is one admitted compile and its
processes; it moves through the lifecycle `RUNNING`, `DRAINING` and
`SERVER_DRAIN` (`buildlimits/admission_state.py`, `GroupState`). A group is
_cached_ when it runs with the managed sccache server (the _cache_). A
`SERVER_DRAIN` group has finished but keeps its token because work it handed
to the cache server may still be running (`Group.may_orphan_server_work`).

`buildlimits/admission.py` holds `Admission`, a pure state machine whose event
methods (`submit`, `token_arrived`, `launch_failed`, `cancel`,
`leader_exited`, `group_emptied`, `server_activity`, `health_failed`,
`begin_closing`) return _effects_ from `buildlimits/effects.py` (`RequestToken`,
`ReturnToken`, `Launch`, `NotifyQueued`, `NotifyFinished`, `Refuse`,
`Terminate`, `Shutdown`). The queue is a first-in, first-out `deque`. Whether a
launch is cached is decided at launch: `cacheable and self.cached and
self.cache_allowed()`. `Counts` (`admission_state.py`) includes
`cached_moving`, the number of cached `RUNNING` plus `DRAINING` groups.

`buildlimits/server_watch.py` holds `ServerWatch`. Its drain probe,
`drained_tokens`, asks `ManagedSccache.accounted_drain`
(`buildlimits/sccache.py`) whether the service is stopped and empty; only then
does `Admission.server_activity` release `SERVER_DRAIN` tokens. A failed probe
records a _fault kind_, one of `live_work`, `stopped_populated` or `manager`
(`buildlimits/drain_guidance.py`, `FaultKind`). `hold_cache` denies cache
composition for a period; `cache_allowed` reads that hold.

`buildlimits/cache_recovery.py` holds `CacheRecovery`, the recovery
transaction. Its `Phase` is `idle`, `quiescing` (write the restart barrier),
`stopping` (stop the cache), `accounting` (wait for the retained tokens to be
released) and `restarting` (clear the barrier and start the cache). `request`
opens a transaction for an operator; `tick` opens one automatically when
`recovery_starvation.starvation` finds starvation, and otherwise advances one
phase step. The stop is guarded by `recovery_starvation.blocked` (no cached
group moving, and a readable manager) and by `recovery_roots.stop_verdict`
(visible compiler roots delay an automatic stop for
`RECOVERY_ROOTS_GRACE_SECONDS = 600`). Other timers:
`RECOVERY_SETTLE_SECONDS = 60` after a clean ending,
`RECOVERY_COOLDOWN_SECONDS = 900` after a faulted one,
`RECOVERY_STALE_DRAIN_SECONDS = 300`, and retries of
`min(30 * attempts, 120)` seconds with a restart limit of 8.

`buildlimits/supervisor_polling.py` runs, once a second, the drain probe and
then `CacheRecovery.tick`. Each supervisor decision is therefore a pure
transition executed atomically between observations of the outside world,
which is what makes model checking a good fit.

Existing verification: `tests/test_admission.py` (Hypothesis
`AdmissionMachine`, capacity 3, 300 examples of 60 steps);
`tests/cache_recovery_state_machine.py` and
`tests/test_cache_recovery_properties.py` (recovery machine, 200-second steps,
mutation evidence recorded in a comment at the end of the file);
`tests/cache_recovery_model.py` (every modelled group is cached, which is why
incidents 1 and 3 were invisible); `tests/cache_recovery_harness.py`
(`Harness` and `harness_for`, which drive a real `Supervisor` over `FakePool`
with a hand-driven clock); `tests/test_crosshair.py` (the precedent for
invoking an external checker from pytest).

FizzBee terms used here: a _specification_ (`.fizz` file) declares state in
`action Init` and actions that change it; `atomic` actions run without
interleaving; `require` disables an action whose guard is false; `fair` (weak
fairness) means an action that stays enabled is eventually taken, and
`fair<strong>` means an action enabled infinitely often is eventually taken;
`always`, `eventually always`, `always eventually` and `transition` assertions
state properties. A _lasso_ is a liveness counterexample: a finite prefix
followed by a cycle repeated for ever. Options go in YAML frontmatter between
`---` lines at the top of the file. The checker writes results to an output
directory; `error-graph.json` holds a failing path.

Skills and documents to read before starting: the `execplans` skill (this
plan's format), the `hexagonal-architecture` skill (domain, ports and adapters
in `modelcheck/`), the `python-router` skill and through it
`python-testing`, `hypothesis`, `python-types-and-apis` and
`python-data-shapes`; the `firecrawl-mcp` skill for any further FizzBee
documentation lookup; `docs/rfcs/0001-fizzbee-model-of-admission-and-recovery.md`
(the design); `docs/roadmap.md` phase 1 (task numbering); the developer's
guide §§3, 5, 6, 9 and 11 (the code the model transcribes and the test
conventions); `docs/documentation-style-guide.md` (ADR and RFC format);
`docs/scripting-standards.md` (command-line conventions);
`docs/repository-layout.md`.

## Conformance basis

No Terms of Reference document exists. The governing artefacts are:

- RFC 0001, accepted 2026-10-18, at commit `42f40e4`: design §§1-6
  (identifiers RFC-§1 to RFC-§6), fairness F1 to F7, properties S1 to S9,
  L1 to L4 and R1, incidents I1 to I4 (Table 4), hypotheses H1 to H4,
  workstreams W1 to W4 and gates G1 to G4, open questions OQ1 to OQ12.
- `docs/roadmap.md` phase 1, tasks 1.1.1 to 1.5.3, at `42f40e4`.
- `AGENTS.md` and `docs/documentation-style-guide.md` (governing standards).
- ADR 001 (graceful supervisor restart), relevant only to the excluded closing
  path.
- ADR 004, to be written in EP-M2, for the tooling architecture.

Trace links (requirement, design element, milestone, evidence):

```plaintext
RFC-H1, roadmap 1.1.1        -> EP-M1 -> RFC 0001 open-questions record (pre-fix revisions)
RFC-OQ1..3, OQ6, roadmap 1.1.2 -> EP-M1 -> models/spike/*.fizz, tests/test_fizzbee_spike.py
RFC-OQ4..5, roadmap 1.1.3    -> EP-M1 -> Makefile FIZZBEE_VERSION/FIZZBEE_SHA256, make fizzbee
RFC-§5, RFC-§6               -> EP-M2 -> modelcheck/*, tests/test_modelcheck_*.py, ADR 004
RFC-§1..3 (S1,S2,L1,L3,L4), roadmap 1.1.4 -> EP-M3 -> models/admission/admission.fizz,
                                  tests/test_model_check_e2e.py::test_fixed_configuration_holds
RFC Table 4 I1, I3, roadmap 1.1.5 -> EP-M4 -> models/admission/configurations.toml,
                                  tests/test_model_check_e2e.py::test_known_bad_yields_counterexample
RFC-§5 replay, roadmap 1.1.6 -> EP-M4 -> tests/test_model_replay.py
RFC-H1 comparator, roadmap 1.1.7 -> EP-M4 -> tests/test_recovery_progress_comparator.py
RFC-H1, G1, roadmap 1.1.8    -> EP-M5 -> RFC 0001 "H1 verdict" section
RFC-W2 (S1..S9, L1..L4, R1, I2, I4), roadmap 1.2.x -> EP-M6..EP-M8 -> model, configurations, RFC H2 verdict
RFC-H4, roadmap 1.3.x        -> EP-M7 -> configurations 37d08e5 and HEAD, RFC H4 verdict
RFC-§5 drift/projection, H3, roadmap 1.4.x -> EP-M9..EP-M10 -> tests/test_model_drift.py,
                                  tests/test_model_projection.py, RFC H3 verdict
RFC-§6, W4, roadmap 1.5.x    -> EP-M11..EP-M12 -> make model-check, .github/workflows/model-check.yml,
                                  developer's guide §11.6
```

_Figure 1: Trace links from RFC 0001 and the roadmap to milestones and
evidence._

## Verification plan

This plan has two layers to verify: the model's claims about the design, and
the repository-owned tooling that runs the model and interprets its results.
Third-party internals (FizzBee's checker, Hypothesis, pytest) are axioms; the
repository-owned logic built on them is verified against the real interface.

### Axioms

- A1. FizzBee v0.5.4 explores every reachable state of a specification within
  its configured bounds and reports a liveness violation, under the declared
  fairness, as a failing path. Exercised by the toy-model negative controls in
  EP-M1, which must fail.
- A2. FizzBee prints `PASSED: Model checker completed successfully` only when
  every retained assertion holds. Exercised by EP-M1 fixtures and by the e2e
  negative controls; the parser never relies on the exit status alone.
- A3. The systemd user manager completes accepted jobs and its transport
  faults are finite (RFC F4); sccache's detached work finishes or is stopped
  (F3); requested tokens arrive and are returned (F5). These are claims about
  the environment, stated beside the actions they constrain, and named in
  every report that relies on them.
- A4. The production event loop executes each callback and each ordered tick
  sub-step atomically (developer's guide §§3 and 6). This is what licenses
  `atomic` actions in Table 5 of the RFC; a replay divergence that contradicts
  it falsifies the model, not the code.

### Model obligations (discharged by FizzBee runs)

- Obligation M-S: S1 to S9 hold in every reachable state of the fixed
  configuration. Method: state-machine model checking (exhaustive within
  bounds). Rationale: safety over all interleavings within bounds is exactly
  what random Hypothesis search could not establish. Domain: capacity 3, two
  waiting requests, the W1 then W2 bounds of `models/admission/fizz.yaml`.
  Artefact: `models/admission/admission.fizz` and
  `models/admission/configurations.toml`. Evidence:
  `make model-check` reports `holds` for each property on configuration
  `head`. Non-vacuity: `exists` assertions (FizzBee reachability probes) for a
  stop issued, a token released by accounting, a transaction reaching
  `restarting`, and a group in each lifecycle state, each of which must be
  satisfied; a seeded fault per property (for example, deleting the
  `cached_moving` guard from `RecoveryStop`) must produce a finite
  counterexample to S1, recorded in the model's known-bad sweep.
- Obligation M-L: L1 to L4 hold under F1 to F7 on the fixed configuration.
  Method: liveness model checking with `liveness: strict` set explicitly.
  Rationale: incidents 1 and 3 were infinite behaviours with no finite
  counterexample. Domain: as M-S. Artefact: as M-S. Evidence: `holds` with the
  fairness identifiers listed. Non-vacuity: the known-bad configurations are
  the negative controls (next obligation); additionally a configuration
  `head-without-F1` with the recovery step made unfair must yield a lasso to
  L3, showing the property depends on the stated fairness rather than holding
  trivially.
- Obligation M-KB: each known-bad configuration yields a counterexample of the
  predicted shape to its Table 4 property, and inverting a prediction fails
  the check. Method: model checking plus the expectation judgement of
  `modelcheck.expectations`. Domain: I1 at capacity 3 (lasso to L3 under weak
  and under strong fairness of the recovery step), I1 at capacity 2 (lasso
  under weak, none under strong), I3 at `eba08ad` (lasso to L1, finite
  counterexample to L4), and in W2 I2 (finite counterexample to S8 and R1),
  I4 (finite counterexample to S9) and H4 at `37d08e5` (L1 fails without F3).
  Evidence: the e2e test and `make model-check` report `counterexample as
  expected`. Non-vacuity: by construction each configuration differs from the
  fixed one only in the switches it names, so a pass on the fixed one and a
  failure on the known-bad one isolate the switch; the report prints the
  switch values.

### Tooling obligations (discharged by pytest)

- Obligation T1 (fail-closed classification): for any checker output, the
  parsed outcome is `PASSED` only if the exit status is 0, a pass line is
  present, and no `FAILED`, `DEADLOCK`, `Model checker stopped` or error line
  is present. Method: Hypothesis property test over generated outputs built by
  interleaving real fixture lines with arbitrary text, plus parametrized tests
  over the captured fixtures. Artefact: `tests/test_modelcheck_fizzbee_output.py`.
  Evidence: red first under `xfail(strict=True)`, then passes. Non-vacuity:
  `hypothesis.event` classifies generated cases by presence of each marker
  line; the test asserts every class was reached via a `target`ed or explicit
  example; the mutation "ignore the FAILED line" must be rejected.
- Obligation T2 (judgement truth table): `judge(expectation, outcome)` returns
  `met` exactly when the expectation is `holds` and the outcome is `PASSED`,
  or the expectation is `fails` with the same property and counterexample
  kind (finite or lasso); every other combination is `unmet` with a reason,
  and an `ERROR` or `STOPPED` outcome is never `met`. Method: exhaustive
  parametrized test over the finite product of expectation kinds and outcome
  kinds (the domain is small enough to enumerate). Artefact:
  `tests/test_modelcheck_expectations.py`. Non-vacuity: the table has a row
  for every pair, generated from the enums, so a new enum member fails the
  test until classified.
- Obligation T3 (materializer locality): materializing a configuration changes
  only the named constant lines, the tagged fairness keywords, the frontmatter
  and the removed assertions; materializing with an empty configuration is the
  identity on the body; materializing is idempotent; an unknown constant or
  fairness tag is an error, never silently ignored. Method: Hypothesis
  property tests over generated specification texts (constant lines,
  tagged actions and assertions interleaved with arbitrary lines) and
  configurations. Artefact: `tests/test_modelcheck_materialize.py`.
  Non-vacuity: classification events for each rewrite kind; a seeded mutation
  that rewrites every `fair` keyword, not only tagged ones, must be rejected.
- Obligation T4 (drift completeness): the drift report is empty if and only
  if every inventory item is mapped or explicitly excluded with a reason, and
  every mapped dotted path resolves. Method: Hypothesis property over
  generated inventories, mapping sets and exclusions, plus an integration test
  against the real `buildlimits` inventory. Artefacts:
  `tests/test_modelcheck_drift.py`, `tests/test_model_drift.py`. Non-vacuity:
  the roadmap's success criterion is the negative control: a test that adds
  a fake `Effect` member to the inventory, and one that renames a mapped path,
  must both produce a non-empty report.
- Obligation T5 (trace normalization): a normalized trace, built from
  `error-graph.json`, preserves the order of actions and the model state at
  each step, and marks the cycle start of a lasso. Method: parametrized tests
  over captured `error-graph.json` fixtures from EP-M1 (one finite, one
  lasso). Artefact: `tests/test_modelcheck_trace.py`. Non-vacuity: the lasso
  fixture's cycle start is asserted to differ from the trace start.
- Obligation T6 (replay correspondence): each counterexample trace is replayed
  through the production doubles; every step either matches the projected
  production state or is reported as a divergence naming the step. Method:
  scripted pytest per trace. Artefact: `tests/test_model_replay.py`.
  Non-vacuity: replaying the fixed configuration's `exists` witness traces
  must match at every step; replaying a known-bad trace at HEAD must diverge
  at the step its fix governs (for I1, the step that issues the stop).
- Obligation T7 (projection inclusion, W3): every transition recorded from the
  Hypothesis machines, projected onto model state, is an edge of the model's
  state graph up to stuttering. Method: property test over the existing
  machines with a recording hook, checked against the exported graph at small
  bounds. Artefact: `tests/test_model_projection.py`. Non-vacuity: the test
  records how many distinct projected transitions it saw and fails below a
  floor; a seeded model mutation that removes `TokenArrives` must be rejected.

### Residual gaps

The model checks a design abstraction within bounds. It does not prove the
Python implementation correct. Replay and projection narrow the gap only in
the direction of behaviours the doubles can exhibit. Journal trace checking
(roadmap 2.1.2) and per-group identities are out of scope.

## Plan of work

The work follows RFC 0001's workstreams in order, with a hard stop at each
gate. Each milestone ends with `make check-fmt`, `make typecheck`, `make lint`
and `make test` passing (run sequentially by the `scrutineer` agent), plus
`make markdownlint` and `make nixie` when documentation changed, and, from
EP-M3 on, `make model-check`.

Stage A (EP-M1) settles the facts the design rests on, without production
tooling: pre-fix revisions, a pinned binary, and toy models that answer the
language questions and capture real checker output as test fixtures.

Stage B (EP-M2) builds the `modelcheck` tooling test-first against those
fixtures and a fake checker, so the tooling is trustworthy before any
counterexample is believed.

Stage C (EP-M3 to EP-M5) is the RFC's W1 spike: the minimal model, the known
bad configurations for incidents 1 and 3, replay, the Hypothesis comparator
and the H1 verdict. It stops at G1.

Stage D (EP-M6 to EP-M12) covers W2 to W4 and runs only as each gate decides.

## Milestones and plateaus

### EP-M1: facts, pinned tool and toy models (roadmap 1.1.1 to 1.1.3)

Outcome: the repository can install a verified FizzBee, the pre-fix revisions
are named, and every W1 language question has a runnable answer.

1. Pre-fix revisions (1.1.1). Clone `leynos/dev-env-rocky` into
   `/tmp/dev-env-rocky-history` (scratch only) and search with
   `git log -S cached_moving`, `git log -S RECOVERY_COOLDOWN_SECONDS`,
   `git log -S cooling_for` and `git log -S _last_fault` over the
   `build_limits` paths. Record each incident's last pre-fix commit, or its
   absence with the transcription basis, in a new "Open questions record"
   section of RFC 0001, answering OQ8.
2. Pin (1.1.3). Add to the `Makefile`: `FIZZBEE_VERSION ?= v0.5.4`,
   `FIZZBEE_SHA256 ?= 8a1d0900f9ad920002151922337912e11f9e588a2f1196249fe40bf75370f782`,
   `FIZZBEE_DIR := .tools/fizzbee-$(FIZZBEE_VERSION)`, `FIZZ := $(FIZZBEE_DIR)/fizz`,
   and a `fizzbee` target that downloads
   `fizzbee-$(FIZZBEE_VERSION)-linux_x86.tar.gz` from the GitHub release with
   `curl --fail --location`, checks it with `sha256sum --check`, and unpacks
   it into `$(FIZZBEE_DIR)` atomically (unpack into a temporary sibling, then
   rename). The target is idempotent: if `$(FIZZ)` exists it does nothing.
   Before relying on the pinned digest, re-read the release page and confirm
   it; record the licence (Apache-2.0 for the repository; check the tarball
   for a bundled licence file) in RFC 0001, answering OQ4 and OQ5.
3. Toy models (1.1.2). Under `models/spike/`, write small specifications that
   answer OQ1 (records, dicts, `bag()`, roles, per-role-instance fairness),
   OQ2 (`liveness: strict` against `eventual`, and how a lasso is reported),
   OQ3 (the `error-graph.json` and pass-graph schema) and OQ6 (state counts
   with and without symmetry; partial-order reduction is not documented). Each
   includes a negative control: a liveness property that must fail under weak
   fairness and hold under strong, mirroring the capacity-2 prediction. Copy
   the checker's standard output and `error-graph.json` for one finite and one
   lasso failure, and one pass, into `tests/fixtures/fizzbee/` as the EP-M2
   parser fixtures. Record answers and workarounds (including the interleaving
   points of RFC 0001's sketch) in the RFC's open-questions record.
4. Add `tests/test_fizzbee_spike.py`, marked `fizzbee`, that runs each toy
   model and asserts its documented answer (for example, the weak-fairness
   negative control fails and the strong one holds). Register the `fizzbee`
   marker in `pyproject.toml`.

Acceptance: `make fizzbee` installs and verifies the binary twice in a row
without error (the second run is a no-op); `uv run pytest -m fizzbee
tests/test_fizzbee_spike.py` passes; without the binary the same tests are
skipped with a reason naming `make fizzbee`; the four gates pass.

Conformance check: OQ1 to OQ6 and OQ8 answered or recorded as unanswerable;
no production change; no new Python dependency; the only new external
artefact is the pinned binary under `.tools/`.

Recovery: delete `.tools/fizzbee-*` and rerun `make fizzbee`. Remaining gaps:
the tooling (EP-M2). Compatibility decision: none.

### EP-M2: the `modelcheck` tooling (RFC 0001 §§5-6, ADR 004)

Outcome: a tested hexagon that runs a configuration matrix through a checker
port, judges each result against its expectation, renders a report and checks
drift, with the FizzBee command line and the `buildlimits` inventory as
adapters.

Red first: write `tests/features/model_check.feature` and its step module
`tests/test_model_check_behaviour.py` against a `FakeChecker` double in
`tests/modelcheck_doubles.py`; write the unit and property tests named in
T1 to T5 with `@pytest.mark.xfail(strict=True, reason="EP-M2 red")`; run them
and confirm they fail for the expected reason (`ModuleNotFoundError` or the
unimplemented function). Then implement module by module, removing each
`xfail` as its tests go green. Write ADR 004 in this milestone.

The feature specification:

```gherkin
Feature: Model check verdicts
  The model check passes only when every fixed configuration holds and every
  known-bad configuration still yields the counterexample it was built to find.

  Scenario: A fixed configuration holds
    Given a configuration "head" expected to satisfy "L3"
    And the checker reports that "L3" holds
    When the model check runs
    Then the report shows "head" "L3" as "holds"
    And the model check succeeds

  Scenario: A known-bad configuration yields its counterexample
    Given a configuration "incident-1" expected to fail "L3" with a lasso
    And the checker reports a lasso counterexample to "L3"
    When the model check runs
    Then the report shows "incident-1" "L3" as "counterexample as expected"
    And the model check succeeds

  Scenario: A known-bad configuration loses its counterexample
    Given a configuration "incident-1" expected to fail "L3" with a lasso
    And the checker reports that "L3" holds
    When the model check runs
    Then the report shows "incident-1" "L3" as "expected counterexample missing"
    And the model check fails

  Scenario: A counterexample of the wrong shape does not count
    Given a configuration "incident-3" expected to fail "L4" with a finite trace
    And the checker reports a lasso counterexample to "L4"
    When the model check runs
    Then the model check fails

  Scenario: Uninterpretable checker output fails closed
    Given a configuration "head" expected to satisfy "S1"
    And the checker output has no verdict line
    When the model check runs
    Then the report shows "head" "S1" as "error"
    And the model check fails

  Scenario: Every report names its fairness assumptions
    Given a configuration "head" expected to satisfy "L1"
    And the property "L1" relies on fairness "F1, F3, F6"
    And the checker reports that "L1" holds
    When the model check runs
    Then the report line for "head" "L1" names "F1, F3, F6"

  Scenario: An unmapped effect is drift
    Given the model maps every admission event and effect
    And the code gains an effect "Pause"
    When the drift check runs
    Then it fails naming "buildlimits.effects.Pause"

  Scenario: A renamed mapped function is drift
    Given the model maps "buildlimits.admission.Admission.token_arrived"
    And that path no longer resolves
    When the drift check runs
    Then it fails naming "buildlimits.admission.Admission.token_arrived"
```

Modules (see `Interfaces and dependencies` for signatures): `modelcheck/domain.py`,
`modelcheck/expectations.py`, `modelcheck/materialize.py`, `modelcheck/drift.py`,
`modelcheck/trace.py`, `modelcheck/report.py`, `modelcheck/ports.py`,
`modelcheck/service.py`, `modelcheck/adapters/fizzbee.py`,
`modelcheck/adapters/inventory.py`, `modelcheck/adapters/configuration.py`,
`modelcheck/__main__.py`. Add `"."` to `[tool.pytest.ini_options] pythonpath`
and `"modelcheck"` to `testpaths` so doctests run. Snapshot the rendered
report for the four outcome classes (holds, counterexample as expected,
expected counterexample missing, error) and the drift report with syrupy in
`tests/test_modelcheck_report.py`, paired with semantic assertions on the
verdict and exit status; normalize durations and paths before snapshotting.

Acceptance: `make test` runs the feature, unit, property and snapshot tests
and they pass; each `xfail` was observed failing before its implementation;
`uv run python -m modelcheck --help` prints the commands `check` and `drift`;
the four gates pass; ADR 004 passes `make markdownlint`.

Conformance check: domain modules import nothing from `modelcheck.adapters`,
`subprocess` or `pathlib` I/O (enforced by a test that parses imports,
`tests/test_modelcheck_layering.py`); no production change; no new
dependency. Recovery: the package is additive; revert the milestone commits.
Remaining gaps: no real model yet. Compatibility decision: none.

### EP-M3: the minimal model with a busy queue (roadmap 1.1.4)

Outcome: `models/admission/admission.fizz` checks S1, S2, L1, L3 and L4 at
capacity 3 on configuration `head`, each report naming its fairness.

First, audit `git diff 7445bf0 HEAD -- buildlimits/` for the modules in RFC
0001's update policy and record the result in RFC 0001 (D1). Then write the
model with the RFC's §1 scope, excluding health, start-up, closing,
escalation detail and metrics as W1 requires. State is counts by class: free
tokens, tokens held by others, queue counts of cacheable and uncached
requests, `token_requested`, the tracked request and its position, groups by
lifecycle and composition (and whether each `SERVER_DRAIN` group may orphan
work), the reachability reading, the hold, the drain fault kind and root
evidence (zero, visible, unknown), the retained-drain age as a timer class,
and the transaction phase, attempts (bounded), trigger, last fault and rest
timer. Each supervisor action carries `# maps: <dotted path>` and each fair
action carries `# fairness: <F-id>`. Guards use `require` over state read in
place. Add `models/admission/fizz.yaml` (W1 bounds, `liveness: strict`) and
`models/admission/configurations.toml` with configuration `head` and its
expectations, and `exists` probes for non-vacuity.

Red first: add `tests/test_model_check_e2e.py` (marker `fizzbee`) with
`test_fixed_configuration_holds` parametrized over the `head` expectations,
under `xfail(strict=True)` until the model exists. Add the `model-check`
target: `model-check: fizzbee build` running
`$(UV_ENV) $(UV) run python -m modelcheck check --fizz $(FIZZ) models/admission`
followed by `pytest -m fizzbee`.

Acceptance: `make model-check` prints `holds` for S1, S2, L1, L3 and L4 on
`head`, each with its fairness identifiers, and all `exists` probes are
satisfied; the four gates pass. Conformance check: every Table 5 action of
the RFC that is in W1 scope exists and is mapped; RFC §1 scope respected;
D1 audit recorded. Recovery: model files are additive. Remaining gaps:
known-bad configurations.

### EP-M4: incidents 1 and 3, replay and comparator (roadmap 1.1.5 to 1.1.7)

Outcome: the H1 evidence exists.

1. Known-bad configurations (1.1.5). Add switches to the model:
   `PREFIX_GUARD_COUNTS_UNCACHED` (I1; transcribes the stop guard at the
   pre-fix revision found in EP-M1) and `PREFIX_TRIGGER_EBA08AD` (I3; the
   trigger needs zero visible roots and no cached group moving in the same
   reading, no stale proof, and `request` is refused while a cached compile
   runs, as at `eba08ad`). Add configurations `incident-1-cap3-weak`,
   `incident-1-cap3-strong`, `incident-1-cap2-weak`, `incident-1-cap2-strong`
   (expected to hold: the prediction is that strong fairness of the recovery
   step defeats the capacity-2 lasso), `incident-3` (L1 lasso, L4 finite) and
   `head-without-F1` (L3 lasso). Red: add them to the e2e test before the
   switches exist so the materializer rejects the unknown constant.
2. Replay (1.1.6). Add `tests/model_replay.py`, a driver that maps each model
   action name to a `Harness` call (`tests/cache_recovery_harness.py`) or an
   `Admission` event, advancing `Harness.now` by each timer class's duration,
   and projects the production state to model state after each step. Store
   each counterexample's normalized trace in `models/admission/traces/` (JSON,
   produced by `python -m modelcheck check --save-traces`). Add
   `tests/test_model_replay.py` with one test per stored trace: known-bad
   traces must diverge at HEAD at the step the fix governs; witness traces of
   `head` must replay exactly. A trace the code follows to a property
   violation is a defect: stop and report it (Constraints).
3. Comparator (1.1.7). Add `tests/test_recovery_progress_comparator.py`
   extending the recovery machine with uncached groups and a bounded-progress
   invariant "once a transaction reaches `stopping` with no cached group
   moving, the stop is issued within N supervisor ticks", with N derived from
   the retry schedule (`min(30 * attempts, 120)` seconds) and stated in the
   test. A second test patches the pre-fix guard in and asserts Hypothesis
   finds a failure (`pytest.raises`), recording examples and wall time with
   `--hypothesis-show-statistics`.

Acceptance: `make model-check` reports `counterexample as expected` for each
known-bad expectation and `holds` for `head`; the capacity-2 results match the
prediction or the inversion is reported (Tolerances); every trace has a
replay test; the comparator's cost is recorded beside the model's in
`Artefacts and notes`; the four gates pass. Recovery: revert the switches and
configurations; the fixed model stands.

### EP-M5: H1 verdict and G1 (roadmap 1.1.8)

Add an "H1 verdict" section to RFC 0001 with the evidence table (configuration,
property, predicted, observed, run time, states explored), the comparator
comparison, the replay dispositions, the number of model revisions used, and a
proposed G1 decision (proceed, change tool to TLA+, prefer Hypothesis, or
stop). Tick roadmap tasks 1.1.1 to 1.1.8 as their success criteria are met.
Set this plan's status to `BLOCKED` pending the user's G1 decision, record it
in `Decision log`, and stop.

### EP-M6 to EP-M8: full model, H4 and G2 (W2; roadmap 1.2.x and 1.3.x)

Only after a "proceed" decision at G1. EP-M6 adds the remaining environment
(signals and clean exits, bounded cancellations, detached server work, the
user manager with transport faults, the operator, health, the barrier) and
S3 to S9 (1.2.1, 1.2.2). EP-M7 adds L2 and the abstract timers in small units
advanced by a tick action, R1, incidents 2 and 4 (finite counterexamples to
S8 and R1, and S9), and H4's configurations at `37d08e5` and HEAD (1.2.3 to
1.2.5, 1.3.1, 1.3.2). EP-M8 measures H2's bounds (capacity 3, two waiting
requests, two abnormal endings, one manager fault, restart limit 2) against
the 10- and 60-minute budgets, settles OQ12 (start-up quiescence and the
closing path from ADR 001: in scope or deferred to roadmap 2.1.3), records the
H2 verdict and G2 proposal in RFC 0001, and stops for G2. Each new
counterexample gets a replay test as in EP-M4.

### EP-M9 and EP-M10: honesty checks and G3 (W3; roadmap 1.4.x)

Only after G2. EP-M9 adds `tests/test_model_drift.py`, the drift check over
the real inventory (public `Admission` event methods, `Effect` members, `Phase`
and `FaultKind` literals), with reasoned exclusions for paths out of model
scope; and the regression or correspondence test for every counterexample
from the full model (1.4.1, 1.4.2). EP-M10 adds projection inclusion
(`tests/test_model_projection.py`, obligation T7) using the exported state
graph at small bounds, then records the H3 verdict and G3 proposal (required
or advisory) and stops.

### EP-M11 and EP-M12: quality gate and G4 (W4; roadmap 1.5.x)

Only after G3. EP-M11 finalizes `make model-check` (routine bounds plus every
known-bad configuration; a `MODEL_BOUNDS=nightly` variant), checks two
consecutive runs on unchanged sources give identical reports, and adds
`.github/workflows/model-check.yml`: on pull requests touching `models/`,
`modelcheck/` or the update-policy modules, and nightly with larger bounds,
caching `.tools/fizzbee-<version>` keyed on version and digest. EP-M12
documents the model (developer's guide §11.6, Table 1 row, §13 CI bullet;
repository layout entries for `models/` and `modelcheck/`, and `.tools`
wording; contents entry for ADR 004), records the G4 decision on joining
`make all` and on making the job required (OQ10), and stops.

## Concrete steps

All commands run from the repository root,
`/home/leynos/.lody/repos/github---leynos---buildlimits/worktrees/3b0cd6e0-5780-46a7-a5d4-6d223b4e16e6`
or the worktree that replaces it. Gate logs go to `/tmp` with the naming
`/tmp/$ACTION-buildlimits-$(git branch --show-current).out`.

Install and verify the checker (EP-M1):

```bash
make fizzbee 2>&1 | tee /tmp/fizzbee-buildlimits-$(git branch --show-current).out
.tools/fizzbee-v0.5.4/fizz models/spike/weak_fairness.fizz
```

Expected tail of the second command for the weak-fairness negative control:

```plaintext
FAILED: Liveness check failed
Invariant: EventuallyServed
```

Run the gates after each milestone (delegated to `scrutineer`):

```bash
make check-fmt 2>&1 | tee /tmp/check-fmt-buildlimits-$(git branch --show-current).out
make typecheck 2>&1 | tee /tmp/typecheck-buildlimits-$(git branch --show-current).out
make lint 2>&1 | tee /tmp/lint-buildlimits-$(git branch --show-current).out
make test 2>&1 | tee /tmp/test-buildlimits-$(git branch --show-current).out
make markdownlint 2>&1 | tee /tmp/markdownlint-buildlimits-$(git branch --show-current).out
make nixie 2>&1 | tee /tmp/nixie-buildlimits-$(git branch --show-current).out
make model-check 2>&1 | tee /tmp/model-check-buildlimits-$(git branch --show-current).out
```

Expected shape of the model-check report from EP-M4 on (illustrative; the
exact wording is pinned by the syrupy snapshot):

```plaintext
head                    S1  holds                        fairness: none
head                    L3  holds                        fairness: F1, F2, F5, F6
incident-1-cap3-weak    L3  counterexample as expected   lasso, 9 steps
incident-3              L4  counterexample as expected   finite, 6 steps
model check: 14 expectations met, 0 unmet
```

Commit after each green step with a message in the imperative mood, after the
gates pass, and push to the draft pull request.

## Validation and acceptance

Quality criteria:

- Tests: `make test` passes, including the BDD feature, the T1 to T6 unit and
  property tests and the report snapshots; `make model-check` passes, with
  every `fizzbee`-marked test run rather than skipped.
- Verification: M-S, M-L and M-KB discharged for W1's scope at G1 and for the
  full scope at G2; T1 to T6 discharged in EP-M2 to EP-M4; T7 at G3.
- Lint and typecheck: `make check-fmt`, `make lint` and `make typecheck` exit
  0.
- Documentation: `make markdownlint` and `make nixie` exit 0.
- Performance: routine bounds under 10 minutes and nightly under 60 minutes
  on the development host and on a hosted runner (G2, G4).
- Security: the binary is fetched over HTTPS from the pinned release and its
  SHA-256 digest is checked before unpacking; no shell strings are built from
  input.

Red-Green-Refactor evidence is recorded in `Artefacts and notes` per
milestone: the red command and its expected failure (strict `xfail` or
`ModuleNotFoundError`), the green command, and the refactor gate run.

## Idempotence and recovery

`make fizzbee` is idempotent and atomic: a partial download leaves only a
temporary directory, which the next run removes. Model checks write to a
fresh `out/` directory under `/tmp` or the configured output directory, never
into `models/`. Every milestone is additive, so recovery is reverting its
commits. Trace files in `models/admission/traces/` are regenerated by
`python -m modelcheck check --save-traces` and reviewed as diffs.

## Artefacts and notes

To be filled during implementation: red and green transcripts, the pre-fix
revision table, the toy-model answers, state counts and run times, the
comparator's cost, and the replay dispositions.

## Interfaces and dependencies

No new dependencies. FizzBee v0.5.4 (Apache-2.0) is an external tool pinned in
the `Makefile`. The package layout and the contracts that must exist at the
end of EP-M2 are below.

```plaintext
modelcheck/
├── __init__.py
├── __main__.py            driving adapter: Cyclopts CLI, composition root
├── domain.py              value types: Outcome, CounterexampleKind, CheckResult, Configuration
├── expectations.py        Expectation, Judgement, judge()
├── materialize.py         materialize(): source text + Configuration -> spec text
├── drift.py               parse_mappings(), drift() -> DriftReport
├── trace.py               Trace, Step, normalize_trace()
├── report.py              render_report(), render_drift()
├── ports.py               ModelChecker, CodeInventory, ConfigurationSource protocols
├── service.py             run_matrix(): the application service over the ports
└── adapters/
    ├── fizzbee.py         FizzbeeChecker (subprocess) and parse_output()
    ├── inventory.py       BuildlimitsInventory (importlib, typing.get_args)
    └── configuration.py   TomlConfigurations (tomllib over configurations.toml)
models/
├── spike/                 EP-M1 toy models
└── admission/
    ├── admission.fizz     the model
    ├── fizz.yaml          default bounds and liveness mode
    ├── configurations.toml fixed and known-bad configurations with expectations
    └── traces/            normalized counterexample and witness traces
```

_Figure 2: Planned layout of the tooling and models._

The ports, in `modelcheck/ports.py`:

```python
class ModelChecker(typ.Protocol):
    def check(self, spec_text: str, *, work_dir: Path) -> CheckResult: ...

class CodeInventory(typ.Protocol):
    def items(self) -> frozenset[str]: ...
    def resolves(self, dotted_path: str) -> bool: ...

class ConfigurationSource(typ.Protocol):
    def source_text(self) -> str: ...
    def configurations(self) -> tuple[Configuration, ...]: ...
```

The domain and service contracts:

```python
class Outcome(enum.StrEnum):
    PASSED = "passed"
    COUNTEREXAMPLE = "counterexample"
    STOPPED = "stopped"
    ERROR = "error"

class CounterexampleKind(enum.StrEnum):
    FINITE = "finite"
    LASSO = "lasso"

@dc.dataclass(frozen=True, slots=True)
class CheckResult:
    outcome: Outcome
    property_name: str | None
    kind: CounterexampleKind | None
    trace: Trace | None
    states_explored: int | None
    detail: str

@dc.dataclass(frozen=True, slots=True)
class Expectation:
    configuration: str
    property_name: str
    fails_as: CounterexampleKind | None   # None means the property must hold
    fairness: tuple[str, ...]

def judge(expectation: Expectation, result: CheckResult) -> Judgement: ...
def materialize(source: str, configuration: Configuration, *, keep: str) -> str: ...
def drift(mapped: frozenset[str], inventory: CodeInventory,
          exclusions: cabc.Mapping[str, str]) -> DriftReport: ...
def run_matrix(source: ConfigurationSource, checker: ModelChecker,
               work_dir: Path) -> tuple[Judgement, ...]: ...
```

Hexagonal boundaries: `domain`, `expectations`, `materialize`, `drift`,
`trace` and `report` are pure and import only the standard library and each
other. `service` depends on `ports` and the domain. Only `adapters/*` and
`__main__` touch processes, files or `buildlimits`. `__main__` is the only
place that constructs adapters. `tests/test_modelcheck_layering.py` enforces
this by parsing imports.

## Revision note

- 2026-10-24: first draft from Wyvern reconnaissance and FizzBee research.
  Pending community-of-experts review.
