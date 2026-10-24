# Model compile admission and cache recovery in FizzBee

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis` and `Verification plan` must be
kept up to date as work proceeds.

Status: APPROVED

Approval: approved by the user on 2026-10-24 with the instruction to mark this
plan approved before implementation begins. Approval authorizes:

- milestones EP-M1 to EP-M6, which deliver RFC 0001 workstream W1 and stop at
  gate G1;
- the edits to the accepted RFC 0001 that those milestones require (an
  open-questions record, the baseline audit and the H1 verdict); and
- the three proposed deviations DV1 to DV3 in `Decision log`, which refine the
  RFC's baseline and predictions before any trial runs.

Each later gate, G1 to G4, requires a separate explicit decision recorded in
`Decision log`, as RFC 0001's acceptance requires. On a "proceed" decision at a
gate, this plan is first revised so that the next workstream's milestones carry
full template fields, red tests and acceptance evidence, and that revision is
approved before work starts.

## Purpose / big picture

Four defects in compile admission's cache recovery reached the shared
development host on 2026-10-18 before any test found them:

- a recovery that never stopped the cache while a busy queue refilled each
  freed slot;
- a rest that left retained tokens idle without explanation;
- a trigger whose evidence never coincided under load;
- a fault that vanished from status.

Each was a defect of behaviour over time. The existing Hypothesis state
machines and CrossHair contracts check safety over short random interleavings.
None checks progress (liveness: "something good eventually happens"), none
models a busy queue, and none composes admission with recovery.

[RFC 0001](../rfcs/0001-fizzbee-model-of-admission-and-recovery.md), accepted
on 2026-10-18, proposes a FizzBee model of the token pool, admission, the
managed cache, the drain probe and the four-phase recovery transaction. FizzBee
is an open-source model checker whose specification language is close to
Python. It explores every reachable state of a model within stated bounds, and
checks safety and liveness properties under declared fairness. Fairness is an
assumption that an action which stays enabled is eventually taken.

After this plan is delivered, a maintainer can run one command:

```bash
make model-check
```

The command prints one line per expectation. Each line names the configuration,
the property and the verdict, and lists the fairness assumptions in force and
the number of states explored. It fails when any of these happens:

- a fixed configuration violates a property;
- a deliberately broken ("known-bad") configuration that transcribes a
  historical incident stops producing its counterexample;
- an exploration is truncated or collapses below its recorded size;
- the checker's output cannot be interpreted.

A companion pytest suite fails when the code gains an admission event, effect,
recovery phase or fault kind that the model does not map. It also replays every
counterexample through the production `Admission`, `ServerWatch` and
`CacheRecovery` objects.

Nothing changes for users of the installed package. The wheel, runtime
behaviour, thresholds and interfaces are untouched (RFC 0001 non-goals).

## Constraints

- **No production changes.** No runtime behaviour, threshold, configuration
  key, command-line interface, status document, metric or journal field in
  `buildlimits/` may change. The model checks the design; it designs nothing
  (RFC 0001 non-goals; H4, "Decision: none here").
  - A genuine counterexample against HEAD is reported to the user as a defect,
    with a replayed failing test.
  - Fixing it is a separate, separately approved change.
- **Packaging.** The wheel's contents must not change:
  `[tool.hatch.build.targets.wheel] packages = ["buildlimits"]` stays.
  - The source distribution will include `modelcheck/` and `models/`. That is
    acceptable, and the package metadata does not change.
- **No new Python dependencies** without escalation.
  - The tooling uses the standard library: `subprocess`, `os`, `signal`,
    `tomllib`, `json`, `pathlib`, `importlib`, `ast`, `typing`, `enum` and
    `dataclasses`.
  - It may also use packages already in the `dev` group: `cyclopts`, `pytest`,
    `pytest-bdd`, `hypothesis` and `syrupy`.
- **The FizzBee binary is local, pinned and checksum-verified.**
  - It is unpacked into a content-addressed cache directory outside the
    worktree and linked from the git-ignored `.tools/fizzbee`.
  - It is never committed and never installed system-wide.
  - It is never fetched or run by `make test`, `make lint`, `make typecheck`
    or `make check-fmt`.
- **Checker runs never write into tracked directories.**
  - Every run copies its materialized specification into a fresh per-run
    directory under the git-ignored `.model-check/`, and passes `--output-dir`
    inside it. No run reads a `.fizz` file in place: the FizzBee parser writes
    `Spec.json` beside the specification it reads.
  - After any run, `git status --porcelain` must be empty.
- **Known-bad configurations are HEAD plus named clauses.** Each one is the
  fixed model with named switches that restore specific pre-fix clauses.
  - Each switch cites the revision it transcribes (a commit in this repository
    or in `leynos/dev-env-rocky`) in a `# transcribes: <sha>` comment.
  - An audit, recorded in RFC 0001, shows that the rest of the diff to that
    revision does not affect the target property (RFC 0001 open question 8).
  - Where a revision is unrecoverable, the configuration records that fact and
    its transcription basis.
- **Inverted predictions are reported, never tuned away.** W1 is limited to
  two model revisions after its first full run (RFC 0001 W1).
- **Hexagonal boundaries.**
  - Domain and policy modules in `modelcheck/` must not import from its
    adapters, perform input or output, spawn processes, read the clock or
    import `buildlimits`.
  - Adapters implement ports that the domain declares (see
    `Interfaces and dependencies`).
- **House code rules.**
  - Every Python module stays under 400 lines (`AGENTS.md`).
  - Every public function carries a docstring with an example. Doctests run
    under `make test` because pytest uses `--doctest-modules`. Doctest
    examples in adapters must stay pure and never spawn the binary.
  - Tests live under `tests/` only (`AGENTS.md`).
- **Shared-host resources.** Checker runs set `GOMAXPROCS=2` and
  `GOMEMLIMIT=4GiB`, run serially (pytest `-n 0`), and are killed as a whole
  process group on timeout.
- **Gates.** Gates run sequentially, never overlapped, and full gate runs are
  delegated to the `scrutineer` agent (user instructions).
- **Spelling configuration.** `typos.toml` is generated and never edited by
  hand. Spelling exceptions go in `typos.local.toml` as exact or full-line
  patterns.

## Tolerances (exception triggers)

- **Gates.** EP-M6 ends at G1, EP-M9 at G2, EP-M11 at G3 and EP-M13 at G4.
  - At each gate, stop, and record the evidence and the proposed decision in
    RFC 0001 and in `Decision log`.
  - Wait for the user's explicit decision. Do not start the next workstream on
    a recommendation alone.
- **Model revisions.** If W1 needs a third model revision after its first full
  run, stop. This is H1's falsification boundary.
  - A revision forced by a timer-order infeasibility in replay (see T6) counts
    as a revision.
- **Prediction inversion.** If any known-bad configuration fails to produce its
  predicted counterexample, or a fixed configuration fails, after the one
  permitted correction round, stop.
  - Report the trace, the replay disposition and the suspected cause:
    atomicity, timer abstraction or fairness.
- **Runtime budget.** H2 sets the budgets: 10 minutes for the routine
  (pull-request) bounds and 60 minutes for the nightly bounds. If either is
  exceeded on the development host, stop and present H2's three options:
  - run the whole model on pull requests;
  - run it nightly only;
  - split it into a coupling model and a transaction model, and re-measure
    once.
- **Memory.** If any single checker run exceeds 8 GiB peak resident memory,
  stop.
- **Scope.** If any milestone touches more than 25 files or adds more than
  1,500 net lines of Python, stop and escalate. The count excludes `.fizz`
  models, fixtures and snapshots.
- **Production code.** If any change to a file under `buildlimits/` appears
  necessary, stop.
  - This includes adding a metric gauge (RFC 0001 open question 9, deferred to
    roadmap 2.1.1).
- **Dependencies.** If `cuprum` or any other new package is needed, stop and
  escalate (D5).
- **Tool behaviour.** If the pinned FizzBee release cannot express a property
  or fairness assumption the RFC requires, and no recorded workaround exists,
  stop. Report at G1 with the TLA+ fallback.
- **Iterations.** If a gate still fails after three fix attempts, stop and
  escalate with the failing log path.
- **Ambiguity.** If the pre-fix revision of an incident admits two materially
  different transcriptions, stop and present both.

## Risks

- **Baseline drift.**
  - Risk: RFC 0001 names `7445bf0` as the fixed baseline. HEAD (`42f40e4`) has
    changed `admission.py`, `admission_state.py`, `cache_recovery.py`,
    `recovery_starvation.py` and `server_watch.py`, and added
    `recovery_stop.py` (PRs #14, #15 and #21).
  - Severity: medium. Likelihood: certain.
  - Mitigation: DV1 and the EP-M4 baseline audit.
- **Wall-clock time in the harness.**
  - Risk: the replay harness runs `ServerWatch` on wall time.
    `harness_for` sets `supervisor.clock` after construction, but
    `Supervisor.__init__` already passed `clock=self.clock` to `ServerWatch`
    (`buildlimits/supervisor.py:114`). The cache hold and the drain-retry gate
    therefore ignore `Harness.now`.
  - Severity: high (replay would match or diverge for the wrong reason).
    Likelihood: certain.
  - Mitigation: the replay driver also sets
    `supervisor.server.clock = lambda: harness.now`. This is a test-side
    change only. A replay test asserts that the hold expires under `Harness.now`.
- **Exit status 0 on failure.**
  - Risk: `fizz` exits 0 when a check fails; failure is visible only on
    standard output.
  - Severity: high (a silent false pass). Likelihood: certain on FizzBee
    HEAD; to be confirmed for v0.5.4.
  - Mitigation: the parser fails closed (obligation T1). Fixtures for every
    output class come from the pinned binary (EP-M1).
- **Vacuous passes.**
  - Risk: a check passes for a reason other than the property holding:
    - a missing or misspelt retained assertion;
    - a state space collapsed by a bad `Init` guard;
    - an exploration truncated by `max_actions`;
    - liveness checking silently off (FizzBee's documentation says liveness
      checking is off by default, but `main.go` treats an unset value as
      strict).
  - Severity: high. Likelihood: medium.
  - Mitigation:
    - the materializer refuses a configuration whose retained assertions do
      not exist;
    - every run writes all options, including `liveness`, explicitly;
    - every configuration records a `min_states` floor;
    - a fixpoint check confirms that doubling `max_actions` leaves the state
      count unchanged;
    - truncation is classified `STOPPED`;
    - negative controls must fail.
- **Fairness abstraction.**
  - Risk: identifier-free counts give action-level fairness. One weakly fair
    "some leader exits" action lets the adversary exit uncached groups for ever
    while a cached group runs.
  - Severity: high. Likelihood: certain if unaddressed.
  - Mitigation: per-class actions `LeaderExitsCached` and
    `LeaderExitsUncached`, each weakly fair. Under the hold, the count of
    moving cached groups only falls, so per-class weak fairness suffices. The
    EP-M1 toy models confirm FizzBee's per-role-instance fairness in case L2
    needs it.
- **Timer abstraction.**
  - Risk: unordered timer expiry admits orders that a real clock cannot
    produce, and the stop's retry backoff can defeat strong fairness of the
    stop (DV2).
  - Severity: medium. Likelihood: high.
  - Mitigation: model the backoff and the blocked-stop branch explicitly.
    Replay classifies an impossible timer order separately from a divergence.
- **Language limitations.**
  - Risk: FizzBee does not support assertions inside roles, `import`, `del`,
    or tuple unpacking in `for`. Copying state into a local introduces an
    interleaving point.
  - Severity: medium. Likelihood: certain.
  - Mitigation: properties stay top-level, and guards use `require` over state
    read in place. EP-M1 records each workaround with a runnable example.
- **Model size.**
  - Risk: the full model may exceed 400 lines, and FizzBee has no `import`.
  - Severity: low. Likelihood: medium.
  - Mitigation: the materializer concatenates an ordered fragment list (D4).
- **Pre-fix revisions.**
  - Risk: the pre-fix revisions of incidents 1, 2 and 4 predate the import and
    live in `leynos/dev-env-rocky`.
  - Severity: medium. Likelihood: medium.
  - Mitigation: EP-M1 searches that history. An unrecoverable revision is
    recorded with its transcription basis.
  - Resolved (EP-M1 step 1): dev-env-rocky does not carry the revisions, but
    the staging branch `initial-import` does. All three are named in RFC 0001's
    open-questions record, and the risk no longer applies.
- **Tarball size and integrity.**
  - Risk: the Linux release is a 335 MB tarball containing a Go checker, a
    bundled Python parser and a bash wrapper. Its only published integrity
    evidence is the digest shown on the GitHub release page.
  - Severity: medium. Likelihood: certain.
  - Mitigation:
    - a content-addressed cache shared by worktrees;
    - trust-on-first-use recording of the digest, with the date and the URL;
    - a mismatch message forbidding blind digest bumps;
    - the release's container image digest recorded as a fallback pin.
  - Confirmed (EP-M1 step 3): the tag is unsigned and no signature exists, so
    trust-on-first-use is the whole of the evidence for the tarball. The image
    carries SLSA v1 provenance naming its GitHub Actions builder and source
    revision, which the tarball does not. The 675 MiB unpacked cache entry and
    the `python3` runtime dependency are recorded in RFC 0001 open questions 4
    and 5. The risk stands; it is now measured rather than assumed.
- **Shared-host load.**
  - Risk: the checker uses every core and much memory on a host that other
    agents share.
  - Severity: high. Likelihood: medium.
  - Mitigation: `GOMAXPROCS`, `GOMEMLIMIT`, serial runs, process-group kill,
    and the opt-in environment variable (D6).
- **Lint gates.**
  - Risk: the dead-code and lint gates flag Protocol methods, adapters reached
    only from the command-line entry point, and doubles called only from
    `modelcheck/`. Ruff flags `subprocess` use outside tests.
  - Severity: medium. Likelihood: high.
  - Mitigation: EP-M3 budgets Skylos entry points, Vulture `ignore_names`,
    Ruff per-file ignores and duplication allowances, each with its reason,
    and records them in the developer's guide §12.
- **State explosion.**
  - Risk: H2's bounds may be too large to check within budget.
  - Severity: medium. Likelihood: medium.
  - Mitigation: counts by class, symmetry where groups are held in a `bag()`,
    minimal bounds that still reproduce Table 4, and H2's split option.

## Progress

- [x] (2026-10-23 23:40Z) Renamed the branch to
  `fizzbee-model-of-admission-and-recovery`.
- [x] (2026-10-24 00:05Z) Reconnaissance by a Wyvern team (production code,
  test doubles and harnesses, documentation conventions) and FizzBee research
  through Firecrawl.
- [x] (2026-10-24 00:30Z) First draft of this ExecPlan.
- [x] (2026-10-24 01:20Z) Community-of-experts review by four panellists
  (formal methods; Python and hexagonal architecture; operations, CI and supply
  chain; delivery process and documentation). Their findings were folded into
  this revision.
- [x] (2026-10-24 10:40Z) Draft pull request #26 opened for review.
- [x] (2026-10-24 10:40Z) User approval of the plan, including DV1 to DV3. The
  approval is recorded above and in `Decision log`.
- [x] EP-M1 (prototyping): pre-fix revisions, pinned FizzBee, toy models and
  fixtures.
  - [x] (2026-10-24 12:50Z) Step 1, the pre-fix revisions, recorded as RFC 0001
    "Open question 8" with Table 13. The dev-env-rocky history carries only one
    vendored snapshot of the package and no source change between its addition
    and removal, so it cannot name three revisions. The staging branch
    `initial-import` does: its tip `416b1ae` is tree-identical to the squashed
    import `eba08ad`, and it names `9f2c94f` (incident 1), `f1ee9aa` (incident
    2) and `d91cd22` (incident 4). The vendored `d024aaa` snapshot is recorded
    as corroboration.
  - [x] (2026-10-24 13:05Z) Step 2, the pin and installer, in the `Makefile` as
    `FIZZBEE_VERSION`, `FIZZBEE_SHA256`, `FIZZBEE_HOME`, `FIZZBEE_STAMP` and
    `FIZZ`. `make fizzbee` printed the fetch line once and nothing on the second
    run; the digest, the unsafe-member guard and the `FIZZBEE_HOME` suffix guard
    were each exercised against a local fixture asset, and every failure path
    left no tarball behind. The download and unpack happen inside
    `flock ... 9>"$$home.lock"`, so two checkouts share one cache entry.
  - [x] (2026-10-24 11:25Z) Step 3, provenance and licence, recorded as RFC 0001
    "Open question 4" and "Open question 5". The tarball digest is
    trust-on-first-use from 2026-10-24: the tag is unsigned, and the release
    publishes per-asset digests and nothing more. The tarball holds 1,795
    entries and no Apache-2.0 text or `NOTICE`; the licence rests on the
    repository's Apache-2.0 declaration. The plan's image reference
    `ghcr.io/fizzbee-io/fizzbee:v0.5.4` does not exist — the tag is `0.5.4` —
    so the corrected index digest is pinned in the `Makefile` as
    `FIZZBEE_IMAGE`, a fallback that no target uses. A toy model passed under
    Podman `--network=none`, and the parser's `python3` dependency was
    confirmed by removing it from `PATH`.
  - [x] (2026-10-24 13:10Z) Step 4, the toy models, under `models/spike/`: 26
    specs, each answering one of OQ1, OQ2, OQ3, OQ6, truncation or
    determinism, and each with a passing or failing expectation recorded in
    its header. All were run through `tests/fizzbee_spike.py`, which copies a
    spec into pytest's scratch directory before invoking the checker, so no
    run writes beside a tracked file. The questions are answered in RFC 0001
    "Open questions record" as Open question 1, 2, 3 and 6, plus the
    Truncation and Determinism entries. Two negative-control pairs were kept
    that the plan did not name: `continuously_enabled{,_unfair}.fizz` (weak
    fairness does force a continuously enabled action) and
    `interleaving_plain_rollback.fizz` (an assignment before a test is safe
    only in an atomic action).
  - [x] (2026-10-24 15:55Z) Step 5, the spike tests:
    `tests/test_fizzbee_spike.py` (the language and checker questions),
    `tests/test_fizzbee_graphs.py` (the artefacts a run writes and the state
    counts the RFC quotes) and `tests/test_fizzbee_fixtures.py` (the captured
    output pins), 19 tests between them, over the shared
    `tests/fizzbee_spike.py` runner. The `fizzbee` marker is registered in
    `pyproject.toml`, the opt-in skip is in `tests/conftest.py`, and the three
    `.gitignore` entries are added. Eight output classes are captured in
    `tests/fixtures/fizzbee/` (pass, safety failure, lasso, stutter, deadlock,
    truncation-safety, truncation-liveness, parse error) with their standard
    output, exit code and, where one is written, `error-graph.json` and the
    `.dot` files. The captured `graph.dot` files were regenerated verbatim: an
    earlier pass had replaced their pointer ids with `"node0"`, which the
    checker never emits. The tests were split across three modules because a
    single one reached 438 lines against the repository's 400-line limit; the
    first split left 415, which the graphs module's own boundary then brought
    under it. A fourth helper module, `tests/fizzbee_expectations.py`, carries
    the `Expectation` value, `check` and `paired`: the shared assertion shape
    that the duplication gate would otherwise see repeated in every test. An
    `Expectation` names the model, the verdict line, the counts the run must
    explore and, where the question turns on it, the trace the counterexample
    must step through. The module also separates the safety and deadlock
    `FAILED: Model checker failed` line from the liveness
    `FAILED: Liveness check failed` line, which the first version of the tests
    conflated. A fifth module, `tests/test_fizzbee_opt_in.py`, pins the
    opt-in itself in five tests, so the four gate-exercised modules collect
    20 tests between them.
  - [x] (2026-10-24 13:30Z) Step 6, the documentation: the `make fizzbee` row
    in the developer's guide's Table 1, the `fizzbee` marker and
    `BUILD_LIMITS_MODEL_CHECK` sentence in §11.3, the extended `.tools`
    wording in `docs/repository-layout.md`, and the RFC 0001 answers above.
    The RFC's tables were renumbered into document order as Tables 9 to 13 as
    part of that record.
- [ ] EP-M2: `modelcheck` pure domain.
- [ ] EP-M3: `modelcheck` ports, service, adapters and command-line interface.
- [ ] EP-M4: minimal model with the busy queue (W1).
- [ ] EP-M5: incident 1 and 3 known-bad configurations, replay and
  comparator.
- [ ] EP-M6: H1 verdict recorded; stop at G1.

## Surprises & discoveries

- **The pre-fix revisions are in this repository, not in dev-env-rocky.**
  - Observation: dev-env-rocky vendored the package once and never changed its
    source before removing it, so its history cannot name a pre-fix revision.
    Its pickaxe searches find only the vendoring commit for
    `RECOVERY_COOLDOWN_SECONDS` and `_last_fault`, and nothing at all for
    `cached_moving` and `cooling_for`.
  - Evidence: `git log d83f709..0eeebbe -- 'python/build_limits/src/**'` shows
    no source commit; `git diff eba08ad initial-import` is empty, so the
    staging branch is the exact pre-squash history behind PR #9.
  - Impact: roadmap task 1.1.1 is satisfied from `initial-import`, and the
    known-bad configurations cite `9f2c94f`, `f1ee9aa` and `d91cd22`. The risk
    entry that expected an unrecoverable revision no longer applies.
- **`fizz` exits 0 when a check fails.**
  - Observation: this holds for assertion, deadlock and liveness failures.
  - Evidence: FizzBee `main.go` returns nil on its failure branches, and the
    `fizz` wrapper's comment says the binary "doesn't exit non-zero on FAILED"
    (read 2026-10-24 at FizzBee HEAD `ae9d9cf`).
  - Impact: the parser must fail closed (T1).
- **No Python support in FizzBee's model-based testing.**
  - Observation: it supports Go, Java, Rust and TypeScript only (RFC 0001 open
    question 7).
  - Evidence: `mbt/lib` in the FizzBee repository, and its "work in progress"
    page.
  - Impact: counterexample replay stays bespoke.
- **A passing run and a failing run write different files, and only the error
  graph is replayable in a stable form.**
  - Observation: a pass writes `graph.dot`, `nodes_*.pb`,
    `adjacency_lists_*.pb`, `spec_ast.json` and `state_config.json`; a failure
    adds `error-graph.dot`, `error-graph.json`, `error-states.html` and
    `nodes_errors.pb`. `graph.dot` labels every node with a Go pointer
    (`"0x..."`) that changes every run, so only the error graph names states
    and actions in a form a replay can consume. `--experimental_no_graph`
    writes neither and exports `trace.txt` instead.
  - Evidence: a pass and a failure of the same pinned binary, five runs each
    at `GOMAXPROCS` 1 and 4: one distinct masked standard output, one distinct
    `error-graph.json`, exit 0 every time, and pointer-masked `graph.dot`
    equal. `graph.go:299,329` formats node ids with `%p`.
  - Impact: replay reads `error-graph.json`, or `trace.txt` when the run used
    `--experimental_no_graph`; it must not read `graph.dot`. Open question 3
    records both forms. The per-run inventory is the fixture contract.
- **Liveness dispatch is by an exact string, and an unrecognized one checks
  nothing.**
  - Observation: ``""``(absent), `enabled`, `true`, `strict` and `strict/bfs`
    run `CheckStrictLiveness`; `eventual` and `nondeterministic` run
    `CheckFastLiveness` and print "Checking strict liveness with
    nondeterministic checker"; every other string checks no liveness property
    and still prints `IsLive: true`.
  - Evidence: one model per value, one value per row of RFC 0001's Table 10,
    plus the unquoted YAML scalar `liveness: true`, which fails to parse with
    `invalid value for string field liveness: true` and exits 1.
    `state_config.json` reproduces the frontmatter string verbatim and omits
    the key when the field is absent, even though strict liveness still runs.
  - Impact: RFC 0001 Open question 2 names the supported modes, and W1's
    models spell the mode out rather than relying on a default. A model whose
    liveness mode is a typo passes without checking anything.
- **Symmetry is on by default, applies only to `bag()`, and the flag must
  precede the spec.**
  - Observation: two symmetric workers in a `bag()` give 9 reachable, 7
    distinct and 3 terminal states with the default reduction, and 12, 10 and
    4 with `--no-symmetry-reduction`; the same pair in a list stays 12, 10
    and 4 either way. The launcher stops its option loop at the first
    non-option argument, so `fizz spec.fizz --no-symmetry-reduction` silently
    runs reduced.
  - Evidence: `models/spike/symmetry.fizz` run both ways, with the flag on
    each side of the filename. No partial-order reduction exists in v0.5.4;
    `fizz --help` lists no such flag and the source has no POR pass.
  - Impact: RFC 0001 Open question 6 records both facts, and the spike runner
    puts every option before the model path.
- **Truncation is silent and can manufacture either verdict.**
  - Observation: `max_actions` cuts exploration with no warning. At
    `max_actions: 2` a safety model passes although the forbidden state is
    reachable at 3; at `max_actions: 6` a liveness model fails on a stutter the
    bound produced, and passes at 12.
  - Evidence: `truncation_{safety,liveness}{,_reaches}.fizz`.
  - Impact: every W1 model's bound must exceed its reachable depth, or the
    model must carry a termination action; RFC 0001's Truncation entry records
    the rule.
- **FizzBee's output is machine-readable.**
  - Observation: on failure FizzBee writes `error-graph.json`, and on a pass it
    writes node and link JSON. Its parser also writes `Spec.json` beside the
    specification it reads.
  - Evidence: FizzBee `main.go` and the `fizz` wrapper.
  - Impact: traces are machine-readable (open question 3, provisionally), and
    runs must use scratch copies (Constraints).
- **The release publishes no signature, and the image's tag is not the plan's.**
  - Observation: the `v0.5.4` tag is unsigned, so the tarball digest is
    trust-on-first-use. The container image tag is `0.5.4`; the plan's
    `ghcr.io/fizzbee-io/fizzbee:v0.5.4` does not exist in the registry.
  - Evidence: `gh api repos/fizzbee-io/fizzbee/git/tags/<sha>` reports
    `verification.reason: unsigned`; `skopeo list-tags` returns `0.5.3`,
    `latest` and `0.5.4` only.
  - Impact: RFC 0001 records the trust-on-first-use basis, and the `Makefile`
    pins the image by index digest as `FIZZBEE_IMAGE`, a fallback that no
    target uses. The image's amd64 attestation does carry SLSA v1 provenance,
    so a later decision to containerize inherits stronger evidence than the
    tarball path has.
- **A failed `require` rolls back an atomic step only.**
  - Observation: in an `atomic` action a failing `require` discards the whole
    step. In a plain (non-atomic) action every assignment commits and yields
    as it runs, so a later failing `require` leaves the earlier writes in
    place, and another action can run between an assignment and the test that
    follows it.
  - Evidence: five spellings of one guard were run in both kinds of action.
    `seen = v; if seen == 1` fails in both; `if v == 1` and `require v == 1`
    first pass in both; `seen = v; require seen == 1` passes atomically and
    fails plainly (20 nodes); `done = True; require v == 1` passes atomically
    and fails plainly (8 nodes), with the error graph
    `Init {done: False, v: 0}` → `Deliver {done: True, v: 0}`. A plain
    action's internal yield also appears in `error-graph.json` as a link named
    `thread-0` with an empty `Type`.
  - Impact: RFC 0001's sketch rule "guard before committing" is about
    ordering, not textual adjacency, and it bites hardest in a plain action.
    The rule is recorded in RFC 0001 Open question 1, and
    `models/spike/interleaving_plain_rollback.fizz` is the negative control.
    The earlier, over-general reading that `require` always rolls back is
    corrected here.
  - A second consequence, measured after the first: a commit before a guard
    is invisible to a liveness property. `done = True; require v == 1` in a
    plain action satisfies `always eventually done` at 8 nodes while reaching
    `done` true with `v` zero; the same model with the safety assertion
    `not done or v == 1` fails at 3 nodes. W1 therefore states each
    interleaving hazard as a safety assertion and reserves liveness for
    progress.
- **Weak fairness does force a continuously enabled action.**
  - Observation: an action that stays enabled for ever cannot be starved under
    `fair`/`fair<weak>`. An earlier contrary reading came from an unbounded
    counter that a low `max_actions` truncated, so the "starvation" was the
    bound's artefact.
  - Evidence: `continuously_enabled.fizz` passes and
    `continuously_enabled_unfair.fizz` fails on a stutter from the initial
    state; RFC 0001's RC3 entry records the correction.
  - Impact: W1 may rely on weak fairness for a continuously enabled stop, and
    only a gate-cycled action needs `fair<strong>`.
- **The parser needs a system `python3`, not a `python` or a bundled one.**
  - Observation: `parser/parser_bin` starts `#!/usr/bin/env python3`, so the
    host interpreter only runs the stub; the stub then re-executes the bundled
    CPython 3.12.11 by a hard-coded path in its runfiles tree.
  - Evidence: `strace -e trace=execve` shows `/usr/bin/python3` running
    `parser_bin` and then the bundled `bin/python3`; with `python3` removed
    from `PATH` the run stops at `/usr/bin/env: 'python3': No such file or
    directory` and exits 1.
  - Impact: every host that runs the checker needs `python3` on `PATH`. The
    container image supplies its own at `/usr/local/bin/python3`, which is why
    the fallback pin works even where the host lacks one.
- **`fizz` writes `out/` into the directory it is run from.**
  - Observation: the v0.5.4 binary writes `out/run_<timestamp>/graph.dot` and
    the node/link files relative to the current directory, and `Spec.json`
    beside the specification file it reads.
  - Evidence: a smoke run of the pinned digest on a three-state toy spec
    printed `Writen graph dotfile: out/run_2026-10-24_12-58-12/graph.dot` and
    left `clock.json` next to `clock.fizz`.
  - Impact: `models/**/Spec.json` and `models/**/out/` go in `.gitignore` at
    EP-M1 step 5, and every target run uses a scratch directory. Upstream's
    `Writen` above is a typo, so no output matcher may depend on the correct
    spelling of that word; match on `graph dotfile` instead.
- **A liveness counterexample and a safety one print different verdicts.**
  - Observation: a safety failure or a deadlock prints `FAILED: Model checker
    failed` (the safety case appending `. Invariant:  NeverFull`, with two
    spaces), but a liveness failure prints `FAILED: Liveness check failed`
    and never the other line. Every failing run still exits 0.
  - Evidence: the eight captured fixtures in `tests/fixtures/fizzbee/`, and
    RFC 0001 Open question 2's table.
  - Impact: the spike's first assertion helper matched only the
    `Model checker failed` prefix, so five liveness expectations would have
    passed vacuously. The helper now names the two lines separately, and the
    opt-in suite is the only thing that exposed it, since no lint or unit gate
    reads a checker verdict.
- **One supervisor tick is one atomic unit.**
  - Observation: `Supervisor._tick` (`buildlimits/supervisor.py:326-345`)
    reaps leaders, empties scopes and runs `poll_services`, which runs the
    drain probe and then `CacheRecovery.tick` on one `now` sample, all in one
    callback.
  - Evidence: the code at `42f40e4`.
  - Impact: the model uses one composite `SupervisorTick` action (D10), and
    RFC 0001 Table 5's atomic units are refined accordingly.
- **The harness clock does not reach `ServerWatch`.**
  - Evidence: `buildlimits/supervisor.py:114` and
    `tests/cache_recovery_harness.py:176`.
  - Impact: see the wall-clock time entry in `Risks`.
- **`main` has moved past the RFC's baseline in the modelled modules.**
  - Evidence: `git diff --stat 7445bf0 HEAD -- buildlimits/`.
  - Impact: DV1.
- **No technical design document exists.**
  - Observation: there is no `docs/*-design.md`.
  - Impact: D7.
- **The spelling gate only reads tracked files, so new fixtures are invisible
  to it until they are staged.**
  - Observation: the first gate run after `bac76ce` reported 98 `color` and 26
    `Writen` hits under `tests/fixtures/fizzbee/`, plus `normalise` in
    `tests/fizzbee_spike.py` and one `artifacts` docstring. None had been
    flagged in the four runs before that commit, because the gate enumerates
    `git ls-files --stage` and Typos only walks an explicit file list.
  - Evidence: `typos-config-builder gate --scope all` against the untracked
    tree passes; the same command after `git add` reports every hit. The
    fixtures are verbatim checker output compared byte for byte by
    `test_captured_fixtures_match_the_live_output_and_exit_code`, so upstream's
    `Writen` and Graphviz's `color=` cannot be corrected without breaking the
    pin.
  - Impact: `typos.local.toml` excludes `tests/fixtures/fizzbee/` as the one
    place captured external output lives, and the exclusion is narrow enough
    to pass the gate's own "too broad" check. The helper was renamed to
    `normalize`, and the doctest now quotes a stable launcher line instead of
    the misspelled one. Anyone adding a captured fixture must stage it before
    the spelling gate can see it.

## Decision log

### Approval

- **AP1. The plan is approved, including DV1 to DV3.**
  - Decision: the user approved this plan on 2026-10-24 with the instruction to
    mark it approved before implementation begins. The status line above reads
    `APPROVED`.
  - Scope: EP-M1 to EP-M6, the RFC 0001 edits those milestones need, and DV1 to
    DV3. G1 to G4 each still require their own explicit decision.
  - Recorded: 2026-10-24, before any milestone work began.

### Proposed deviations (approved with this plan on 2026-10-24)

- **DV1. Transcribe HEAD; keep `7445bf0` as a named audit point.**
  - Decision: the fixed configuration, `head`, transcribes HEAD of `main` at
    the start of EP-M4 (currently `42f40e4`).
  - Before writing the model, EP-M4 audits
    `git diff 7445bf0 <head> -- buildlimits/` over the update-policy modules,
    and records the result in RFC 0001.
  - The expected finding is that the recovery dynamics are unchanged. The
    changes are refactors into `recovery_outlook.py` and `recovery_stop.py`,
    plus the status-only outlook. Admission gained graceful closing
    (`begin_closing`, the `closing` gate and the manual request in
    `supervisor_shutdown.settle_graceful_stop`), which W1 excludes along with
    start-up and shutdown.
  - If the audit confirms this, `head` also stands for `7445bf0` within W1's
    scope, and RFC 0001's predictions about `7445bf0` are checked on `head`.
  - If the audit finds a behavioural change inside W1's scope, both revisions
    become separate configurations and the plan is set to `BLOCKED`.
  - Affected: RFC 0001 H1, H4 and W2's exit evidence; roadmap tasks 1.1.5,
    1.2.2, 1.2.5, 1.3.2 and 1.3.4. Each will read "`7445bf0`, checked as
    `head` per the baseline audit".
  - Rationale: replay drives the production code at HEAD, so the model must
    transcribe HEAD for replay to mean anything.
  - Approver: the user, through plan approval.
- **DV2. Refine the incident 1 capacity-2 prediction.**
  - The RFC predicts that at capacity 2, incident 1's lasso appears under weak
    but not strong fairness of the recovery step. That holds only in an
    abstraction without the stop's retry backoff.
  - In the code, `_try_stop` is never disabled. A blocked stop calls `_fail`,
    which re-arms the backoff (`min(30 * attempts, 120)` seconds) and resets
    the roots grace. An adversary that expires the backoff only while an
    uncached group is moving keeps the stop branch disabled at every attempt,
    so strong fairness gives no help.
  - The model therefore splits the stop into `RecoveryStopTaken` and
    `RecoveryStopBlocked`. It checks two capacity-2 configurations under
    strong fairness:
    - `incident-1-cap2-strong-nobackoff`, expected to hold, which is the RFC's
      prediction in the RFC's abstraction;
    - `incident-1-cap2-strong-backoff`, expected to yield a lasso.
  - Strong fairness here stands in for a physical assumption the code does not
    guarantee: an unsynchronized 1-second poll eventually lands in a window of
    positive length. The H1 verdict will say so.
  - Without this refinement, a lasso at capacity 2 under strong fairness would
    be misread as wrong atomicity and spend W1's revision budget on the wrong
    cause.
  - Recorded in RFC 0001 before the first run.
- **DV3. Split L4 and add fairness assumption F3a.**
  - L4 is two properties:
    - L4a (safety, finite counterexample): a refused operator request implies
      a faulted manager transport. It is a `transition` assertion.
    - L4b (liveness): an accepted request eventually ends in a completed
      transaction or accounted tokens.
  - Incident 3's "finite counterexample to L4" is a counterexample to L4a.
  - The liveness of the visible-roots grace needs a new assumption F3a:
    unknown root readings are finite. The grace neither starts nor completes
    while the root count is unknown (`recovery_roots.stop_verdict`).
    - F3a is stated beside the drain probe.
    - Configuration `head-unknown-roots`, with F3a dropped, is expected to
      fail L1.
    - This anticipates H4's "a grace that cannot expire" clause.
  - Recorded in RFC 0001 before the first run.

### Decisions

- **D2. The tooling is a top-level package, `modelcheck/`, outside the wheel.**
  - It is organized as a hexagon:
    - pure domain modules;
    - a `ports` module of `typing.Protocol` interfaces;
    - an application service;
    - adapters for the FizzBee process, FizzBee's output format, the
      configuration file and the `buildlimits` code inventory;
    - a Cyclopts driving adapter.
  - The models live in `models/admission/`.
  - Rationale:
    - The tooling has real policy: expectation judgement, materialization of
      variants, drift computation and trace normalization. That policy must
      be unit-tested without the 335 MB binary, and the ports let the
      behavioural scenarios run against a fake checker.
    - `scripts/` is for single-file `uv run --script` programs, and `tests/`
      holds tests and test doubles.
    - Git source discovery, Skylos (`roots=["."]`), Pylint and `ty` already
      cover a top-level package.
  - Recorded as ADR 004 in EP-M3.
- **D3. One expected failure per run.**
  - Each run retains every assertion expected to hold in its configuration,
    plus exactly one expected failure. The other expected failures are
    stripped.
  - All runs of one configuration must report the same `states_explored`.
    That proves cheaply that stripping assertions did not change the state
    graph.
  - Rationale: FizzBee reports the first failing property. A known-bad
    configuration could otherwise fail a different property first, and a
    counterexample found where a safety property is violated would mean
    nothing.
- **D4. Variants are produced by a pure materializer from one source.**
  - It rewrites only:
    - top-level constant lines named in the configuration;
    - the fairness keyword of actions tagged `# fairness: <id>`;
    - the complete YAML frontmatter (every option, including `max_actions`,
      `max_concurrent_actions`, `liveness` and `deadlock_detection`);
    - the set of retained assertions.
  - It concatenates an ordered fragment list first if a model is ever split.
  - It also derives `fairness_in_force` from the tags, so reports cannot drift
    from the model.
  - Rationale: FizzBee has no `import` and no parameters, and hand-copied
    variants would drift from the fixed model.
- **D5. `subprocess` with process-group control.**
  - The FizzBee adapter uses `subprocess.Popen` with an argument list,
    `start_new_session=True`, no shell and `check=False` semantics.
  - On timeout it sends `SIGTERM` to the process group with `os.killpg`, then
    `SIGKILL` after 5 seconds, then reaps the processes and closes the pipes.
  - It is not `cuprum`, because `cuprum` is not a dependency and the
    scripting standards govern `scripts/`. Requiring it would be a dependency
    decision for the user.
  - Ruff `S404` and `S603` are ignored for this one adapter module, with the
    reason recorded.
- **D6. Running the real binary is opt-in.**
  - Tests marked `fizzbee` run only when `BUILD_LIMITS_MODEL_CHECK=1`. This
    follows the host-acceptance precedent of `BUILD_LIMITS_HOST_ACCEPTANCE`.
  - Under that variable, a missing binary is a failure, not a skip.
  - `make model-check` sets the variable, runs pytest with `-n 0 -m fizzbee`,
    and fails if anything was skipped.
  - `make test` never runs these tests, whatever is installed. It stays
    hermetic.
  - Whether `make model-check` joins `make all` is G4's decision (open
    question 10).
- **D7. Where design decisions are recorded.**
  - With no technical design document in the repository, decisions go to
    three places:
    - RFC 0001: the open-questions record, the baseline audit, the H1 to H4
      verdicts and the gate decisions;
    - ADR 004: the tooling architecture;
    - a new developer's guide §11.6: internal interfaces, conventions and the
      update policy.
  - `docs/users-guide.md` is not changed. Nothing user-visible changes, and
    the style guide excludes maintainer-only tooling from that guide.
- **D8. Install from EP-M1 through `make fizzbee`.**
  - Rationale: every W1 task needs a reproducible binary. Roadmap task 1.5.1
    is ticked only when its full success criterion is met, in EP-M12.
  - The cache root is resolved by make, not by the shell:
    `$(or $(XDG_CACHE_HOME),$(HOME)/.cache)` rather than
    `$${XDG_CACHE_HOME:-$$HOME/.cache}`. Make compares the stamp path it is
    given against the file system, and a shell expression is not expanded in a
    prerequisite name, so the shell form would make the stamp unreachable and
    refetch on every run. A comment in the `Makefile` records the reason.
  - `FIZZBEE_ASSET` and `FIZZBEE_URL` are separate variables, so a test can
    point the recipe at a local fixture and exercise the digest and path
    guards without a 335 MiB download.
  - `FIZZBEE_IMAGE` records the container image by index digest. It is a
    fallback pin only: no target pulls or runs it, and adding a container
    runtime to the gates is not W1's business. The plan's `v0.5.4` tag does not
    exist, so the corrected pin is the digest both `0.5.4` and `latest` resolve
    to. RFC 0001 open question 4 records the correction.
- **D9. Create `make model-check` in EP-M4.**
  - It is created ahead of roadmap task 1.5.1, with W1 bounds only.
  - Rationale: W1's own acceptance needs a reproducible command. 1.5.1 later
    adds the routine and nightly bounds, the determinism check and CI.
- **D10. One composite supervisor tick.**
  - The model has one `atomic fair action SupervisorTick` (fairness F1) whose
    body:
    1. empties a nondeterministic subset of draining scopes, applying
       `group_emptied` and its token returns;
    2. then chooses, with `oneof`, between "server poll not due" and "drain
       probe, then one recovery step".
  - `LeaderExitsCached` and `LeaderExitsUncached` (the `SIGCHLD` callback,
    F2), `TokenArrives` (the pool callback, F5), `Submit`, `Cancel` and
    `OperatorRequest` remain separate actions.
  - Rationale: this matches `Supervisor._tick`'s single callback. Separate
    actions would admit paths the code cannot take, and replay would reject
    them.
- **D11. Spike artefacts are kept.**
  - `models/spike/`, `tests/test_fizzbee_spike.py`,
    `tests/test_fizzbee_graphs.py` and `tests/test_fizzbee_fixtures.py` stay
    as permanent checks of the language and checker behaviour the model relies
    on.
  - Rationale: a FizzBee version bump that changes fairness, liveness or
    output semantics must fail loudly.
- **D12. A Python explicit-state explorer is offered at G1, not built in W1.**
  - The formal-methods panellist proposed a third comparator arm: a roughly
    300-line Python explorer over the same count-based state. Its transitions
    would call the production pure predicates directly
    (`recovery_starvation.starvation`, `blocked`, `recovery_roots.stop_verdict`
    and `cooldown_after`), with a fair-cycle check by strongly connected
    components.
  - Rationale: RFC 0001 fixes the comparator as the Hypothesis extension, and
    W1's scope is bounded. The option is recorded for the G1 decision, where
    it could pre-discharge much of H3.

- **D13. The spike runner puts every checker option before the model path.**
  - `tests/fizzbee_spike.py::run_model` builds `[fizz, *options, model]`,
    because v0.5.4's launcher stops parsing options at the first non-option
    argument. A flag after the filename is silently ignored, so
    `fizz symmetry.fizz --no-symmetry-reduction` runs with the reduction on.
  - Rationale: an ignored flag is a test that asserts the wrong thing and
    passes. RFC 0001 Open question 6 records the behaviour.
- **D14. Replay reads the error graph, never `graph.dot`.**
  - A replay consumes `error-graph.json` (or the `trace.txt` an
    `--experimental_no_graph` run exports) and checks the verdict line as well
    as the exit status, because every verdict exits 0.
  - Rationale: `graph.dot` names nodes by pointer, so it differs every run and
    cannot be compared or replayed, and the exit status alone cannot tell a
    pass from a failure. `error-graph.json` is stable across runs and carries
    the action, state and fairness of each step.

## Outcomes & retrospective

Not started. Completed at each gate and at completion.

### EP-M1

- **Delivered.** The pin and installer in the `Makefile`; the pre-fix
  revisions and the release provenance and licence recorded in RFC 0001; 26 toy
  models and 20 spike tests; eight captured output classes; the developer's
  guide, repository layout and RFC 0001 open-questions record updated. Roadmap
  tasks 1.1.1 to 1.1.3 are ticked.
- **What the spike changed.** Three RFC 0001 statements moved on evidence: the
  capacity-2 refinement (RC1, DV2), the split of L4 into L4a and L4b with the
  addition of F3a (RC2, DV3), and the correction that weak fairness does force
  a continuously enabled action (RC3). The guard rule in the RFC's sketch is
  now scoped to atomic actions, with a plain-action negative control.
- **What it cost.** No W1 revision budget was touched: the spike's models are
  not W1's, and the tolerance's two revisions count W1's model after its first
  full run. One spike reading was wrong and was replaced: weak fairness was
  first read as not forcing an enabled action, from a truncated unbounded
  counter, and `continuously_enabled.fizz` corrected it before it reached the
  RFC.
- **Carried forward.** Nothing blocking. `TY_VERSION ?= 0.0.74` in the
  `Makefile` and the developer's guide's `ty` version lag `ty==0.0.89` in
  `pyproject.toml`; both are recorded for a separate change, not this plan.
  Open questions 7 and 9 to 12 stay open and none bears on W1.

## Context and orientation

The repository is a Python 3.14 package, `buildlimits/`. It runs a supervisor
that admits Rust compiler invocations into a shared token pool, so that only a
few compile at once.

**Admission terms.**

- A _token_ is one compile slot from the shared pool (`buildlimits/pool.py`).
  `FakePool` in `tests/pool_helpers.py` is its test double.
- A _request_ is a compile waiting for a token.
- A _group_ is one admitted compile and its processes. It moves through the
  lifecycle `RUNNING`, `DRAINING` and `SERVER_DRAIN`
  (`buildlimits/admission_state.py`, `GroupState`).
- A group is _cached_ when it runs with the managed sccache server (the
  _cache_).
- A `SERVER_DRAIN` group has finished but keeps its token, because work it
  handed to the cache server may still be running
  (`Group.may_orphan_server_work`, which is true unless the leader exited
  cleanly and was not cancelled).

**The admission state machine.** `buildlimits/admission.py` holds `Admission`,
a pure state machine.

- Its event methods are `submit`, `token_arrived`, `launch_failed`, `cancel`,
  `leader_exited`, `group_emptied`, `server_activity`, `health_failed` and
  `begin_closing`.
- They return _effects_ from `buildlimits/effects.py`: `RequestToken`,
  `ReturnToken`, `Launch`, `NotifyQueued`, `NotifyFinished`, `Refuse`,
  `Terminate` and `Shutdown`.
- The queue is a first-in, first-out `deque`. At most one token request is
  outstanding.
- Whether a launch is cached is decided at launch, as
  `cacheable and self.cached and self.cache_allowed()`.
- `Counts` includes `cached_moving`, the number of cached `RUNNING` plus
  `DRAINING` groups.

**The drain probe.** `buildlimits/server_watch.py` holds `ServerWatch`.

- Its drain probe, `drained_tokens`, asks `ManagedSccache.accounted_drain`
  (`buildlimits/sccache.py`) whether the service is stopped and empty. Only
  then does `Admission.server_activity` release the `SERVER_DRAIN` tokens.
- A failed probe records a _fault kind_: `live_work`, `stopped_populated` or
  `manager` (`buildlimits/drain_guidance.py`, `FaultKind`).
- A `live_work` fault carries a root count. Zero proves "no visible roots",
  a positive count shows roots, and an unknown count proves neither.
- `hold_cache` denies cache composition for a period, and `cache_allowed`
  reads that hold.

**The recovery transaction.** `buildlimits/cache_recovery.py` holds
`CacheRecovery`.

- Its `Phase` is one of:
  - `idle`;
  - `quiescing` (write the restart barrier);
  - `stopping` (stop the cache);
  - `accounting` (wait until the retained tokens are released);
  - `restarting` (clear the barrier and start the cache).
- `request` opens a transaction for an operator, and is refused only on a
  `manager` fault.
- `tick` renews the hold, then does one of two things:
  - opens a transaction automatically when `recovery_starvation.starvation`
    finds starvation (the `stale` or `detached` proof);
  - otherwise advances one phase step.
- The stop is guarded by `recovery_starvation.blocked`: no cached group may be
  moving, and the manager must be readable. A blocked stop counts as a failure:
  it increments the attempts, arms a backoff of `min(30 * attempts, 120)`
  seconds, and resets the roots grace.
- `recovery_roots.stop_verdict` delays an automatic stop while roots are
  visible, for `RECOVERY_ROOTS_GRACE_SECONDS = 600`.
- The other timers are:
  - `RECOVERY_SETTLE_SECONDS = 60` after a clean ending;
  - `RECOVERY_COOLDOWN_SECONDS = 900` after a faulted ending;
  - `RECOVERY_STALE_DRAIN_SECONDS = 300`;
  - a 120-second hold renewed on every tick;
  - a restart limit of 8.

**Polling.** `buildlimits/supervisor.py` runs `_tick` as one callback. It reaps
leaders, empties draining scopes, and then calls
`supervisor_polling.poll_services`. That runs the drain probe and then
`CacheRecovery.tick` on one `now` sample, once a second.

**Existing verification.**

- `tests/test_admission.py`: the Hypothesis `AdmissionMachine` (capacity 3,
  300 examples of 60 steps).
- `tests/cache_recovery_state_machine.py` and
  `tests/test_cache_recovery_properties.py`: the recovery machine with
  200-second steps. Mutation evidence is recorded in a comment at the end of
  the properties module.
- `tests/cache_recovery_model.py`: every modelled group is cached, which is
  why incidents 1 and 3 were invisible.
- `tests/cache_recovery_harness.py`: `Harness` and `harness_for` drive a real
  `Supervisor` over `FakePool` with a hand-driven clock.
- `tests/test_crosshair.py`: the precedent for invoking an external checker
  from pytest.

**FizzBee terms.**

- A _specification_ (`.fizz` file) declares state in `action Init`, and
  actions that change it.
- `atomic` actions run without interleaving. `require` disables an action
  whose guard is false.
- `fair` (weak fairness) means an action that stays enabled is eventually
  taken. `fair<strong>` means an action enabled infinitely often is eventually
  taken.
- `always`, `exists`, `eventually always`, `always eventually` and
  `transition` assertions state properties. An `exists` assertion is a
  reachability probe.
- A _lasso_ is a liveness counterexample: a finite prefix followed by a cycle
  repeated for ever. A _stutter_ counterexample ends in a state where nothing
  more happens.
- Options go in YAML frontmatter between `---` lines.

Skills and documents to read before starting:

- **Skills:**
  - `execplans`: this plan's format and living-document duties.
  - `hexagonal-architecture`: domain, ports and adapters in `modelcheck/`.
  - `python-router`, and through it `python-testing`, `hypothesis`,
    `python-verification`, `python-types-and-apis`, `python-data-shapes` and
    `python-errors-and-logging`.
  - `ruff-016` and `skylos`: the lint gates.
  - `firecrawl-mcp`: further FizzBee documentation lookup.
  - `en-gb-oxendict`: prose.
  - `commit-message`.
  - `sdlc-implementation`: executing an approved plan.
- **Documents:**
  - RFC 0001 (the design).
  - `docs/roadmap.md` phase 1 (task numbering).
  - The developer's guide: §§3, 5, 6 and 9 (the code the model transcribes);
    §11.3 (existing verification); §12 (static-analysis exceptions); §13
    (automation); §14 (spelling).
  - ADR 001 (the excluded closing path).
  - ADR 003 (the format model for ADR 004).
  - `docs/documentation-style-guide.md`.
  - `docs/scripting-standards.md`.
  - `docs/repository-layout.md`.

## Conformance basis

No Terms of Reference document exists. The governing artefacts, at commit
`42f40e4`, are:

- **RFC 0001**, accepted 2026-10-18. Its identifiers are:
  - design sections RFC-§1 to RFC-§6;
  - fairness F1 to F7, plus F3a (DV3);
  - properties S1 to S9, L1 to L3, L4a, L4b (DV3) and R1;
  - incidents I1 to I4 (Table 4);
  - hypotheses H1 to H4, workstreams W1 to W4 and gates G1 to G4;
  - open questions OQ1 to OQ12.
- **The roadmap**, `docs/roadmap.md` phase 1, tasks 1.1.1 to 1.5.3.
- **Governing standards:** `AGENTS.md` and
  `docs/documentation-style-guide.md`.
- **ADR 001**, relevant only to the excluded closing path.
- **ADR 004**, to be written in EP-M3.
- **Deferred items:** OQ9 is deferred to roadmap 2.1.1 and OQ11 to roadmap
  2.1.2. Neither is discharged here.

For screen readers: the following block lists one trace link per line. Each
line runs from a roadmap task, through its RFC identifiers and the milestone
that delivers it, to its acceptance evidence.

```plaintext
1.1.1 OQ8                     -> EP-M1  -> RFC 0001 open-questions record: pre-fix revision table
1.1.2 OQ1-3, OQ6              -> EP-M1  -> models/spike/*.fizz; tests/test_fizzbee_{spike,fixtures}.py; tests/fixtures/fizzbee/
1.1.3 OQ4-5                   -> EP-M1  -> Makefile FIZZBEE_VERSION, FIZZBEE_SHA256; make fizzbee; RFC licence record
RFC-§5, §6 tooling            -> EP-M2  -> modelcheck domain; tests/test_modelcheck_{expectations,materialize,drift,trace}.py
RFC-§5, §6 tooling            -> EP-M3  -> modelcheck adapters, CLI; tests/features/model_check.feature; ADR 004
1.1.4 S1 S2 L1 L3 L4a L4b F1-F7 F3a -> EP-M4 -> models/admission/admission.fizz; .model-check/results.json (head)
1.1.5 I1 I3 H1 DV2            -> EP-M5  -> models/admission/configurations.toml; results.json (known-bad rows)
1.1.6 RFC-§5 replay           -> EP-M5  -> tests/test_model_replay.py
1.1.7 H1 comparator           -> EP-M5  -> tests/test_recovery_progress_comparator.py
1.1.8 H1 G1                   -> EP-M6  -> RFC 0001 "H1 verdict" section; Decision log G1 entry
1.2.1 S1 S2 S4 S5 S7          -> EP-M7  -> results.json (head, W2 bounds)
1.2.2 S3 S6 S8 S9             -> EP-M7  -> results.json (head)
1.2.3 L1-L4b F1-F7            -> EP-M8  -> results.json (head, fairness ablations)
1.2.4 R1 I2                   -> EP-M8  -> results.json (incident-2)
1.2.5 Table 4 I4              -> EP-M8  -> results.json (incident-4, all known-bad rows)
1.2.6 H2 G2                   -> EP-M9  -> RFC 0001 "H2 verdict" section
1.3.1 H4 (37d08e5)            -> EP-M8  -> results.json (stale-path-37d08e5)
1.3.2 H4 (head grace)         -> EP-M8  -> results.json (head, head-unknown-roots)
1.3.3 OQ12                    -> EP-M9  -> RFC 0001 OQ12 decision; start-up action or roadmap 2.1.3 deferral
1.3.4 W2 exit                 -> EP-M9  -> RFC 0001 W2 exit record
1.4.1 RFC-§5 drift            -> EP-M10 -> tests/test_model_drift.py
1.4.2 OQ7 regressions         -> EP-M10 -> tests/test_model_replay.py (full model); RFC OQ7 record
1.4.3 projection              -> EP-M11 -> tests/test_model_projection.py
1.4.4 H3 G3                   -> EP-M11 -> RFC 0001 "H3 verdict" section
1.5.1 RFC-§6 pin and target   -> EP-M12 -> make model-check (routine, nightly, determinism)
1.5.2 CI                      -> EP-M12 -> .github/workflows/model-check.yml
1.5.3 OQ10 docs               -> EP-M13 -> developer's guide §11.6 update policy; G4 entry
```

_Figure 1: Trace links from the roadmap and RFC 0001 to milestones and
evidence._

## Verification plan

Two layers need verifying:

- the model's claims about the design;
- the repository-owned tooling that runs the model and interprets its results.

Third-party internals (FizzBee's checker, Hypothesis, pytest) are axioms. The
repository-owned logic built on them is verified against the real interface.

### Axioms

- **A1. FizzBee explores exhaustively within bounds.** FizzBee v0.5.4
  explores every reachable state of a specification within its configured
  bounds. It reports a liveness violation, under the declared fairness, as a
  failing path.
  - Exercised by: the EP-M1 negative controls, which must fail; the
    `max_actions` fixpoint check; the `min_states` floors.
- **A2. The pass line means every retained assertion held.** FizzBee prints
  `PASSED: Model checker completed successfully` only when the exploration
  completed and every retained assertion held.
  - Exercised by: the EP-M1 fixtures for every output class, including
    truncation.
  - The parser never relies on the exit status alone.
- **A3. Environment claims.** These describe the environment, not
  buildlimits:
  - the systemd user manager completes accepted jobs, and its transport faults
    are finite (F4);
  - sccache's detached work finishes or is stopped (F3);
  - unknown root readings are finite (F3a);
  - requested tokens arrive and are returned (F5).
  - Each is stated beside the action it constrains, and every report that
    relies on one names it.
- **A4. Atomicity.** The production event loop runs each callback atomically:
  a `SIGCHLD`, pool or connection callback, or one `Supervisor._tick`
  (developer's guide §§3 and 6).
  - This licenses `atomic` actions, and the composite `SupervisorTick` (D10).
  - A replay divergence that contradicts it falsifies the model, not the
    code.

### Model obligations (discharged by FizzBee runs)

- **Obligation M-S: safety on the fixed configuration.**
  - Statement: the safety properties in scope (S1 and S2 in W1; S1 to S9 in
    W2) hold in every reachable state of `head`, within bounds.
  - Method: exhaustive state-machine model checking.
  - Rationale: random Hypothesis search could not establish safety over all
    interleavings.
  - Domain: capacity 3; two waiting requests; W1 bounds in `fizz.yaml` and in
    the materialized frontmatter.
  - Artefacts: `models/admission/admission.fizz` and
    `models/admission/configurations.toml`.
  - Evidence: `make model-check` reports `holds` for each property on `head`,
    with `states_explored` at or above the configuration's `min_states`.
    Doubling `max_actions` leaves the state count unchanged.
  - Non-vacuity:
    - `exists` probes must all be satisfied: a stop issued; a token released
      by accounting; a transaction reaching `restarting`; a group in each
      lifecycle state; a cached and an uncached group running together.
    - Seeded faults must fail. Each is a configuration that deletes one guard,
      such as the `cached_moving` clause of `RecoveryStopTaken`, which must
      yield a finite counterexample to S1.
- **Obligation M-L: liveness on the fixed configuration.**
  - Statement: the liveness properties in scope (L1, L3, L4b in W1; L1 to L4b
    in W2) hold on `head` under F1 to F7 and F3a.
  - Method: liveness model checking, with `liveness: strict` written
    explicitly.
  - Rationale: incidents 1 and 3 were infinite behaviours with no finite
    counterexample.
  - Evidence: `holds`, with the fairness identifiers derived from the tags.
  - Non-vacuity: targeted mutants of `head` must each yield a lasso:
    - `head-no-stale-proof` (L1);
    - `head-no-grace-deadline` (L1, with roots that stay visible);
    - `head-unfair-leader-exits` (L3);
    - `head-unknown-roots` (L1; F3a dropped).
  - At G1, a per-property ablation drops each fairness identifier in turn,
    and the report records which assumptions each property was found to need.
- **Obligation M-KB: the known-bad configurations.**
  - Statement: each known-bad configuration yields a counterexample of the
    predicted kind to its Table 4 property. Losing one fails the check.
  - Method: model checking, plus the expectation judgement in
    `modelcheck.expectations`.
  - Domain:
    - I1 at capacity 3: a lasso to L3 under weak and under strong fairness of
      the recovery step.
    - I1 at capacity 2, under DV2's refinement: a lasso under weak fairness;
      none under strong fairness without the backoff; a lasso under strong
      fairness with the backoff.
    - I3 at `eba08ad`: a lasso to L1, and a finite counterexample to L4a.
    - In W2: I2 (finite counterexamples to S8 and R1), I4 (a finite
      counterexample to S9), and H4 at `37d08e5` (L1 fails without F3).
  - Evidence: the report shows `counterexample as expected`, with the kind.
  - Non-vacuity: each configuration differs from `head` only in the switches
    it names, and the report prints them. D3's equal-state-count check shows
    that the graph is the one `head` explores under those switches.

### Tooling obligations (discharged by pytest)

- **Obligation T1: classification fails closed.**
  - Statement: a run is classified `Passed` only if:
    - the exit status is 0;
    - a pass line is present;
    - no `FAILED`, `DEADLOCK`, truncation, `Model checker stopped` or error
      line is present.
  - Method: an exhaustive parametrized test over all 32 presence combinations
    of the five marker classes, with the oracle derived from which markers
    were inserted. Hypothesis only injects noise lines that cannot start with
    a marker. Parametrized tests also cover the captured EP-M1 fixtures.
  - Artefact: `tests/test_modelcheck_fizzbee_output.py`.
  - Evidence: red under a strict `xfail`, then green.
  - Non-vacuity: the 32 rows are generated from the marker enumeration, so a
    new marker fails the test until it is classified. A hand-applied mutation
    that ignores the `FAILED` line was rejected; it is recorded in a closing
    comment, in the style of `tests/test_cache_recovery_properties.py`.
- **Obligation T2: the judgement truth table.**
  - Statement: `judge` returns a `Verdict` for every combination of
    expectation kind and result variant.
    - `Holds` is met only by `Passed`.
    - `FailsAs(kind)` is met only by a `Counterexample` with the same property
      and kind.
    - `Reachable` is met only by a satisfied probe.
    - `Stopped` and `Errored` are never met.
  - Method: an exhaustive parametrized test over the finite product, generated
    from the enumerations.
  - Artefact: `tests/test_modelcheck_expectations.py`.
  - Non-vacuity: generated rows, so a new variant fails until classified.
- **Obligation T3: the materializer changes only what it names.**
  - Statement:
    - It changes only the named constant lines, the tagged fairness keywords,
      the frontmatter and the stripped assertions.
    - An empty configuration that keeps every assertion is the identity on
      the body.
    - It is idempotent.
    - An unknown constant, fairness tag or retained assertion is an error,
      never silently ignored.
    - It fails unless the retained expected failure names exactly one
      existing assertion.
  - Method: Hypothesis property tests over generated specification texts and
    configurations.
  - Artefact: `tests/test_modelcheck_materialize.py`.
  - Non-vacuity: explicit `@example`s for each rewrite kind. A handwritten
    mutant materializer that rewrites every `fair` keyword, not only the
    tagged ones, is run by the same properties in a test that expects them to
    fail.
- **Obligation T4: drift detection.**
  - Statement:
    - Adding an unmapped inventory item makes the report non-empty and names
      the item.
    - Excluding it with a reason clears it, and mapping it clears it.
    - Mapping a path that does not resolve names that path.
  - Method: metamorphic Hypothesis properties over generated inventories,
    plus an integration test against the real `buildlimits` inventory.
  - Artefacts: `tests/test_modelcheck_drift.py` and, from EP-M10,
    `tests/test_model_drift.py`.
  - Non-vacuity: the roadmap's success criterion is the negative control. A
    fake `Effect` added to the inventory, and a renamed mapped path, must
    each produce a non-empty report.
- **Obligation T5: trace normalization.**
  - Statement: a normalized trace keeps the order of actions and the model
    state at each step. It marks the cycle start of a lasso, and the terminal
    state of a stutter.
  - Method: parametrized tests over captured `error-graph.json` fixtures (one
    finite, one lasso, one stutter).
  - Artefact: `tests/test_modelcheck_trace.py`.
  - Non-vacuity: the lasso's cycle start differs from the trace start, and
    the stutter's last state repeats.
- **Obligation T6: replay correspondence.**
  - Statement: each stored trace is replayed through the production doubles.
    Every step either matches the projected production state or is reported
    with one of three dispositions:
    - a divergence naming the step;
    - a timer order that a real clock cannot produce;
    - an expected divergence at a fixed clause.
  - Method: one scripted pytest per trace.
  - Artefact: `tests/test_model_replay.py`, with the driver in
    `tests/model_replay.py`.
  - Non-vacuity:
    - the `exists` witness traces of `head` must replay exactly;
    - each known-bad trace must diverge at HEAD at the step its fix governs
      (for I1, the step that issues the stop);
    - a replay test asserts that the cache hold expires under `Harness.now`,
      so the clock fix is not vacuous.
- **Obligation T7: projection inclusion (W3).**
  - Statement: every transition recorded from the Hypothesis machines,
    projected onto model state, is an edge of the model's state graph, up to
    stuttering.
  - Method: a property test with a recording hook, checked against the
    exported graph at small bounds.
  - Artefact: `tests/test_model_projection.py`.
  - Non-vacuity: the test fails below a floor of distinct projected
    transitions. A seeded model mutation that removes `TokenArrives` must be
    rejected.
- **Obligation T8: layering.**
  - Statement: pure modules import only an allowlist of standard-library
    modules (`enum`, `dataclasses`, `typing`, `collections.abc`, `re`, `json`)
    and each other.
  - They call no `open`, `read_text`, `write_text`, `subprocess`, `os` or
    `time`.
  - Only `adapters/inventory.py` imports `buildlimits`.
  - Method: an abstract-syntax-tree test.
  - Artefact: `tests/test_modelcheck_layering.py`.
  - Non-vacuity: a fixture module that violates each rule must be rejected.

### Residual gaps

The model checks a design abstraction within bounds. It does not prove the
Python implementation correct. Replay and projection narrow the gap only for
behaviours the doubles can exhibit. Strong fairness of the stop stands in for a
physical timing assumption (DV2). Journal trace checking (roadmap 2.1.2) and
per-group identities are out of scope.

## Plan of work

The work follows RFC 0001's workstreams in order, with a hard stop at each
gate. Every milestone ends with these gates, run in sequence by the
`scrutineer` agent:

- `make check-fmt`, `make typecheck`, `make lint` and `make test`;
- `make markdownlint` and `make nixie`, when documentation changed;
- from EP-M4 on, `make model-check`.

Every milestone also updates the documentation that describes what it added,
and ticks the roadmap tasks whose success criteria it met. The stages are:

- **Stage A (EP-M1, prototyping)** settles the facts without production
  tooling: the pre-fix revisions, a pinned binary, toy models, and captured
  checker output.
- **Stage B (EP-M2, EP-M3)** builds `modelcheck` test-first against those
  fixtures and a fake checker, so the tooling is trustworthy before any
  counterexample is believed.
- **Stage C (EP-M4 to EP-M6)** is the RFC's W1 spike. It stops at G1.
- **Stage D (EP-M7 to EP-M13)** covers W2 to W4. It runs only as each gate
  decides, and only after this plan has been expanded for the next workstream.

## Milestones and plateaus

### EP-M1 (prototyping): facts, pinned tool and toy models

- **Outcome.**
  - The repository can install a verified FizzBee.
  - The pre-fix revisions are named.
  - Every W1 language question has a runnable answer.
  - Real checker output is captured as fixtures.
- **Requirements and gaps:** roadmap 1.1.1 (OQ8), 1.1.2 (OQ1 to OQ3, OQ6),
  1.1.3 (OQ4, OQ5).
- **Steps.**
  1. **Pre-fix revisions.**
     - Clone `leynos/dev-env-rocky` into `/tmp/dev-env-rocky-history`, as
       scratch only.
     - Search the `build_limits` paths with `git log -S cached_moving`,
        `git log -S RECOVERY_COOLDOWN_SECONDS`, `git log -S cooling_for` and
        `git log -S _last_fault`.
     - Record each incident's last pre-fix commit, the clauses that commit
       contributes, and the audit of the rest of its diff. Put this in a new
       "Open questions record" section of RFC 0001.
  2. **Pin.** Add to the `Makefile`:
     - `FIZZBEE_VERSION ?= v0.5.4`;
     - `FIZZBEE_SHA256 ?= 8a1d0900f9ad920002151922337912e11f9e588a2f1196249fe40bf75370f782`;
     - `FIZZBEE_HOME ?= $${XDG_CACHE_HOME:-$$HOME/.cache}/buildlimits/fizzbee/$(FIZZBEE_SHA256)`;
     - `FIZZ ?= .tools/fizzbee/fizz`;
     - a stamp file target, `$(FIZZBEE_HOME)/.verified`, that contains the
       digest.

     The recipe:
     - runs under `flock` on `$(FIZZBEE_HOME).lock`;
     - downloads to `.part` with `curl --fail --location --retry 3`,
       `--retry-all-errors`, `--connect-timeout 20` and `--max-time 900`;
     - checks the digest with `sha256sum --check`;
     - unpacks with `tar --no-same-owner --no-same-permissions` into a
       `mktemp -d` sibling on the same filesystem, rejecting absolute and
       `..` members;
     - moves the result into place with `mv -T`, writes the stamp, deletes
       the tarball, and links `.tools/fizzbee` to `$(FIZZBEE_HOME)`.

     A digest mismatch fails with "upstream asset changed; do not update
     FIZZBEE_SHA256 without review". The `fizzbee` phony target depends on
     the stamp, so a second run is a no-op.
  3. **Provenance and licence.**
     - Record in RFC 0001 the digest as trust-on-first-use, with the date and
       the release URL.
     - Record the tarball's file listing, and every bundled LICENSE or NOTICE
       file (the repository is Apache-2.0, and the bundled CPython is under
       the PSF licence).
     - Record the digest of the container image as `FIZZBEE_IMAGE`, a fallback
       pin that is not used by default. This step named the tag `v0.5.4`;
       the registry publishes `0.5.4` with no `v`, so the pin is the index
       digest that tag resolves to, and RFC 0001 open question 4 records the
       correction.
     - If rootless Podman permits it, run one toy model with `--network=none`
       to show that the tool needs no network.
  4. **Toy models,** under `models/spike/`.
     - Each answers one question:
       - OQ1: records, dicts, `bag()`, roles, and per-role-instance fairness.
       - OQ2: `liveness: strict` against `eventual`, and how a lasso and a
         stutter are reported.
       - OQ3: the `error-graph.json` schema and the schema of the graph
         written on a pass.
       - OQ6: state counts with and without symmetry. Partial-order reduction
         is not documented.
       - Truncation: what v0.5.4 prints when `max_actions` cuts exploration
         short.
       - Determinism: five runs each under `GOMAXPROCS=1` and
         `GOMAXPROCS=4`.
     - Negative controls: a liveness property that must fail under weak
       fairness and hold under strong, and one that must fail with liveness
       explicitly on.
     - Copy each output class into `tests/fixtures/fizzbee/`, with standard
       output and `error-graph.json` where present: pass, safety failure,
       lasso, stutter, deadlock, truncation and parse error.
  5. **Spike tests.**
     - Add `tests/test_fizzbee_spike.py`, marked `fizzbee`, with explicit
       `@pytest.mark.timeout` values. It runs each toy model through a
       minimal scratch-directory runner and asserts the documented answer.
       The captured-output pins went into a second module,
       `tests/test_fizzbee_fixtures.py`, and the artefact and state-count
       pins into a third, `tests/test_fizzbee_graphs.py`, so each stays under
       the 400-line limit. The shared assertion shape lives in
       `tests/fizzbee_expectations.py`: an `Expectation` names the model, the
       verdict line and the counts it must explore, and `check` and `paired`
       enforce one model or a pass/fail pair.
     - Register the `fizzbee` marker in `pyproject.toml`, and add the opt-in
       skip in `tests/conftest.py`.
     - Add `.model-check/`, `models/**/Spec.json` and `models/**/out/` to
       `.gitignore`.
  6. **Documentation.**
     - Developer's guide: a `make fizzbee` row in Table 1; a sentence in
       §11.3 on the `fizzbee` marker and `BUILD_LIMITS_MODEL_CHECK`.
     - `docs/repository-layout.md`: extend the `.tools` wording to "`.tools`
       (the pinned duplication detector and a link to the FizzBee cache)".
     - Record the answers and workarounds, including the interleaving points
       of RFC 0001's sketch, in the RFC's open-questions record.
- **Acceptance evidence.**
  - EP-M1-E1: `make fizzbee` succeeds twice in a row, and the second run
    prints nothing.
  - EP-M1-E2:
    `BUILD_LIMITS_MODEL_CHECK=1 uv run pytest -n 0 -m fizzbee
    tests/test_fizzbee_spike.py tests/test_fizzbee_graphs.py
    tests/test_fizzbee_fixtures.py` passes.
  - EP-M1-E3: without the variable, the same tests are skipped with a reason
    naming it.
  - EP-M1-E4: `git status --porcelain` is empty afterwards.
  - EP-M1-E5: the four gates pass.
  - Roadmap tasks 1.1.1 to 1.1.3 are ticked.
  - **Observed 2026-10-24 13:31Z.** E1: `make fizzbee` exited 0 twice and the
    second run printed nothing (0 bytes). E2: 20 tests passed in 17.50 s with
    `BUILD_LIMITS_MODEL_CHECK=1`, across both test modules. E3: without the
    variable all 20 skipped, each reporting "needs the pinned FizzBee checker;
    set BUILD_LIMITS_MODEL_CHECK=1 (after `make fizzbee`)". E4:
    `git status --porcelain` showed only the milestone's own tracked and
    untracked files, and no run directory.
  - **Observed 2026-10-24 16:28Z, E5.** Scrutineer ran the six gates in
    sequence and all passed:

    | Gate | Exit | Decisive line |
    | ---- | ---- | ------------- |
    | `make check-fmt` | 0 | `29 files left unchanged` (mdtablefix); Ruff `257 files already formatted` |
    | `make typecheck` | 0 | `All checks passed!` |
    | `make lint` | 0 | `duplication gate passed (nose 0.20.0); 133 allowed by reasoned exceptions` |
    | `make test` | 0 | `1775 passed, 45 skipped`; `46 snapshots passed` |
    | `make markdownlint` | 0 | `Summary: 0 issues in 0 files`; spelling gate exit 0, `typos.toml` unchanged |
    | `make nixie` | 0 | `All diagrams validated successfully!` |

    The first gate run, at 15:10Z, failed three of them, all on work this
    milestone had added. `check-fmt` wanted an mdtablefix rewrap and a table
    column widened in `docs/developers-guide.md`; `markdownlint` reported
    MD060 on that same row, MD049 for two asterisk-emphasized words in RFC
    0001, and the spelling gate for six American or non-`artefact` spellings
    across four documents; and `lint` failed the duplication gate on two
    families. `make fmt` fixed the first two. The duplication families were
    fixed by folding the expected trace into the `Expectation` value, which
    removed the `counterexample` helper and let the two weak-fairness tests
    merge into one parametrized contrast, and by a reasoned allow entry for
    the purely matching import lists of the two test modules — the same
    treatment eight existing entries already give that shape.
    `git status --porcelain` was byte-identical before and after every run,
    and no `out/`, `run_*`, `Spec.json` or `.model-check/` appeared.
  - **Milestone scope, measured 2026-10-24 16:40Z.** Sixteen in-scope files
    (ten tracked, six new Python modules) and 1,197 net Python lines (1,165 in
    the new modules, 33 added and one removed in `tests/conftest.py`). The 26
    `.fizz` models, the 33 fixture files and the two snapshots are excluded by
    the plan's scope rule.
- **Keep or discard.** The spike models and tests are kept (D11). Toy-model
  questions that cannot be answered are recorded as unanswerable, with their
  consequence for W3.
- **Conformance check.**
  - OQ1 to OQ6 and OQ8 are answered, or recorded as unanswerable.
  - The design is unchanged, apart from facts feeding DV1 to DV3.
  - No upstream assumption is falsified, or the falsification is recorded.
  - No new Python dependency or public interface.
  - The only new trust boundary is the pinned binary and its digest.
  - Trace links for 1.1.1 to 1.1.3 are current.
- **Recovery:** `rm -rf .tools/fizzbee "$FIZZBEE_HOME"`, never `.tools/`
  itself, then rerun `make fizzbee`.
- **Remaining gaps:** the tooling.
- **Compatibility decision:** none.

### EP-M2: the `modelcheck` pure domain

- **Outcome:** the pure modules exist and are tested, with no input or output:
  - `domain.py`: the result union, expectation kinds, `Verdict` and
    `Configuration`;
  - `expectations.py`: `judge`;
  - `materialize.py`;
  - `drift.py`;
  - `trace.py`;
  - `report.py`.
- **Requirements and gaps:** the tooling side of RFC-§5 and §6 (obligations
  T2 to T5, T8).
- **Red first.**
  - Write `tests/test_modelcheck_expectations.py`,
    `tests/test_modelcheck_materialize.py`, `tests/test_modelcheck_drift.py`,
    `tests/test_modelcheck_trace.py` and
    `tests/test_modelcheck_layering.py`.
  - Each test imports inside its body, and carries
    a strict `xfail` whose `raises` names `ModuleNotFoundError` and
    `NotImplementedError`, with the reason "EP-M2 red".
  - Run `uv run pytest tests/test_modelcheck_*.py`, and expect every test to
    report `XFAIL`.
  - Implement module by module, removing each `xfail` as its tests turn green.
- **Documentation:** add `modelcheck/` and `models/` to the repository layout
  tree and table.
- **Acceptance evidence.**
  - EP-M2-E1: the red run shows only `XFAIL` results; the green run shows them
    passing.
  - EP-M2-E2: the doctests in each module pass under `make test`.
  - EP-M2-E3: the four gates pass.
- **Conformance check.**
  - T2 to T5 and T8 are discharged.
  - The domain imports only the allowlist.
  - No upstream assumption has changed.
  - No new dependency.
  - No new trust boundary.
  - Trace links are current.
- **Recovery:** the package is additive, so revert the milestone's commits.
- **Remaining gaps:** ports, adapters and the command-line interface.
- **Compatibility decision:** none.

### EP-M3: ports, service, adapters and command-line interface

- **Outcome:** the remaining modules exist, and `python -m modelcheck`
  works end to end against a stub checker:
  - `ports.py`;
  - `service.py`;
  - `adapters/fizzbee_output.py`: `parse_output` and `normalize_trace`, both
    pure;
  - `adapters/fizzbee_process.py`;
  - `adapters/configuration.py`;
  - `adapters/inventory.py`;
  - `cli.py`, with `make_app`;
  - `__main__.py`.
- **Requirements and gaps:** RFC-§5 and §6 (obligation T1, plus the
  behavioural contract); ADR 004.
- **Red first.**
  - Write `tests/features/model_check.feature` and
    `tests/test_model_check_behaviour.py`, with a module-level `pytestmark`
    strict `xfail`.
  - `tests/modelcheck_doubles.py` holds `FakeChecker`, which returns
    `parse_output` applied to a named fixture from `tests/fixtures/fizzbee/`.
    The scenarios therefore exercise the real parser against real output.
  - Write `tests/test_modelcheck_fizzbee_output.py` (T1),
    `tests/test_modelcheck_cli.py` and `tests/test_modelcheck_report.py`.
  - Run
    `uv run pytest tests/test_model_check_behaviour.py tests/test_modelcheck_fizzbee_output.py`,
    and expect `XFAIL` results.
- **Feature specification.**

  ```gherkin
  Feature: Model check verdicts
    The model check passes only when every fixed configuration holds and every
    known-bad configuration still yields the counterexample it was built to find.

    Scenario: A fixed configuration holds
      Given a configuration "head" expected to satisfy "L3"
      And the checker output is the fixture "pass"
      When the model check runs
      Then the report shows "head" "L3" as "holds"
      And the model check exits with status 0

    Scenario: A fixed configuration fails
      Given a configuration "head" expected to satisfy "S1"
      And the checker output is the fixture "safety-failure"
      When the model check runs
      Then the report shows "head" "S1" as "unexpected counterexample"
      And the model check exits with status 1

    Scenario: A known-bad configuration yields its counterexample
      Given a configuration "incident-1" expected to fail "L3" with a lasso
      And the checker output is the fixture "lasso"
      When the model check runs
      Then the report shows "incident-1" "L3" as "counterexample as expected"
      And the model check exits with status 0

    Scenario: A known-bad configuration loses its counterexample
      Given a configuration "incident-1" expected to fail "L3" with a lasso
      And the checker output is the fixture "pass"
      When the model check runs
      Then the report shows "incident-1" "L3" as "expected counterexample missing"
      And the model check exits with status 1

    Scenario: A counterexample of the wrong kind does not count
      Given a configuration "incident-3" expected to fail "L4a" with a finite trace
      And the checker output is the fixture "lasso"
      When the model check runs
      Then the report shows "incident-3" "L4a" as "wrong counterexample kind"
      And the model check exits with status 1

    Scenario: A truncated exploration is not a pass
      Given a configuration "head" expected to satisfy "S2"
      And the checker output is the fixture "truncated"
      When the model check runs
      Then the report shows "head" "S2" as "stopped"
      And the model check exits with status 1

    Scenario: Uninterpretable checker output fails closed
      Given a configuration "head" expected to satisfy "S1"
      And the checker output has no verdict line
      When the model check runs
      Then the report shows "head" "S1" as "error"
      And the model check exits with status 1

    Scenario: An exploration below its floor is not a pass
      Given a configuration "head" with a floor of 1000 states expected to satisfy "S1"
      And the checker output is the fixture "pass" with 300 states explored
      When the model check runs
      Then the report shows "head" "S1" as "state space below floor"
      And the model check exits with status 1

    Scenario: An unsatisfied reachability probe fails
      Given a configuration "head" expected to reach "StopIssued"
      And the checker reports that "StopIssued" was never satisfied
      When the model check runs
      Then the report shows "head" "StopIssued" as "unreachable"
      And the model check exits with status 1

    Scenario: Every report names its fairness assumptions
      Given a configuration "head" expected to satisfy "L1"
      And the model tags fairness "F1, F3, F6" on the actions "L1" relies on
      And the checker output is the fixture "pass"
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

- **Command-line contract** (`modelcheck/cli.py`).
  - Commands: `check MODEL_DIR`, with options `--fizz PATH`,
    `--bounds routine|nightly`, `--only CONFIG[:PROPERTY]`, `--json PATH` and
    `--save-traces`; and `drift MODEL_DIR`.
  - Configuration is environment-first, with the prefix `MODELCHECK_` (per the
    scripting standards).
  - Exit codes: 0 when every expectation is met, 1 when any is unmet, 2 on a
    tool or usage error.
  - It is tested through `make_app(...)` with Cyclopts'
    `result_action="return_value"`.
- **End-to-end test, without the binary.**
  `tests/test_modelcheck_cli_e2e.py` runs
  `uv run python -m modelcheck check tests/fixtures/modelcheck/model` as a
  subprocess, with `--fizz` pointing at a stub `fizz` script in
  `tests/fixtures/modelcheck/`. The stub prints a named fixture and writes
  `error-graph.json`. The test asserts:
  - exit status 0 for a met matrix, and 1 when a known-bad configuration
    loses its counterexample;
  - the `--json` file's contents;
  - that the stub saw a scratch-directory path, never the fixture directory.
- **Process control test.** It uses a stub that spawns a sleeping child and
  never exits. The adapter must kill the whole group at its timeout, and the
  test asserts that no descendant survives.
- **Snapshots.** Syrupy snapshots in `tests/test_modelcheck_report.py` cover
  the rendered report for every `Verdict`, and the drift report. Each is paired
  with assertions on the verdicts and the exit status. Durations and paths are
  normalized before snapshotting.
- **Configuration and gates.**
  - Add `"."` to `[tool.pytest.ini_options] pythonpath`, and `"modelcheck"` to
    `testpaths`.
  - Add Ruff per-file ignores for `modelcheck/adapters/fizzbee_process.py`
    (`S404`, `S603`) and for the runtime `Path` annotation in `cli.py`
    (`TC`), with their reasons.
  - Add Skylos entry points for the `modelcheck.__main__` path, and Vulture
    `ignore_names` for the doubles' Protocol methods, each with its call path.
  - Add duplication allowances only if the gate reports them.
- **Documentation.**
  - Write ADR 004, "Model-check tooling as a hexagonal package outside the
    wheel", in the format of ADR 003, and add it to `docs/contents.md`.
  - Add a developer's guide §11.6 stub, "Model checking", covering how to run
    the tooling, the `fizzbee` marker, and the package's ports.
  - Record each lint exception in §12.
- **Acceptance evidence.**
  - EP-M3-E1: the red run shows `XFAIL` results; the green run passes all
    twelve scenarios.
  - EP-M3-E2: `uv run python -m modelcheck --help` lists `check` and `drift`.
  - EP-M3-E3: the CLI end-to-end and process-control tests pass under
    `make test`.
  - EP-M3-E4: the four gates, `make markdownlint` and `make nixie` pass.
- **Conformance check.**
  - T1 and T8 are discharged, and the behavioural contract is met.
  - The ports match `Interfaces and dependencies`.
  - No upstream assumption has changed.
  - No new dependency.
  - The new trust boundary (process spawning) is confined to one adapter.
  - Trace links are current.
- **Recovery:** revert the milestone's commits; EP-M2 still stands.
- **Remaining gaps:** no real model yet.
- **Compatibility decision:** none.

### EP-M4: the minimal model with a busy queue

- **Outcome:** `models/admission/admission.fizz` checks S1, S2, L1, L3, L4a
  and L4b at capacity 3 on configuration `head`, with every report naming its
  fairness.
- **Requirements and gaps:** roadmap 1.1.4; RFC-§1 to §3; F1 to F7 and F3a;
  DV1 and DV3.
- **Steps.**
  1. **Baseline audit (DV1).** Audit `git diff 7445bf0 HEAD` over the
     update-policy modules, and record the result in RFC 0001. Stop if a
     W1-scope behaviour changed.
  2. **Write the model.** Exclude health, start-up, closing, escalation detail
     and metrics, as W1 requires.
     - **Actions,** each a W1-scope row of RFC 0001 Table 5, refined by D10:
       - `Submit` (cacheable or uncached; maps `Admission.submit`);
       - `TokenArrives` (`Admission.token_arrived`, F5);
       - `Cancel` (`Admission.cancel`, bounded);
       - `LeaderExitsCached` and `LeaderExitsUncached`
         (`Admission.leader_exited`, F2, each weakly fair);
       - `SupervisorTick` (F1; `Groups.advance`, `Admission.group_emptied`,
         `ServerWatch.drained_tokens`, `CacheRecovery.tick`), whose recovery
         step has the branches `RecoveryStopTaken` and `RecoveryStopBlocked`;
       - `OperatorRequest` (`CacheRecovery.request`);
       - `ServerWorkEnds` (F3);
       - `TimerExpires` (F6), over the timer classes.
     - **State** is counts by class:
       - free tokens, and tokens held by others;
       - queue counts of cacheable and uncached requests;
       - `token_requested`;
       - the tracked request and its queue position;
       - groups by lifecycle and composition, and whether each
         `SERVER_DRAIN` group may orphan work;
       - the reachability reading, and the hold;
       - the drain fault kind, and the root evidence (zero, visible or
         unknown);
       - the transaction phase, attempts (bounded), trigger and last fault.
     - **Timer classes:** stale (300), grace (600), backoff (30 to 120), rest
       (settle 60 or cooldown 900), hold (120) and drain retry (30). In W1
       each is armed or expired; W2 adds ordered units.
     - **Annotations:**
       - each action carries `# maps: <dotted path>`;
       - each fair action carries `# fairness: <F-id>`;
       - each known-bad switch carries `# transcribes: <sha>`.
     - Guards use `require` over state read in place.
  3. **Configuration.**
     - Add `models/admission/fizz.yaml`, with the W1 bounds and every option
       explicit.
     - Add `models/admission/configurations.toml`, with:
       - `head` and its expectations, `min_states` and `exists` probes;
       - the M-L mutants `head-no-stale-proof`, `head-no-grace-deadline`,
         `head-unfair-leader-exits` and `head-unknown-roots`;
       - the S1 seeded fault, `head-no-cached-guard`.
  4. **Make target.** Add `make model-check: fizzbee build`. Its recipe runs
     the matrix once, then the opt-in tests, and fails if anything was
     skipped:

     ```make
     $(UV_ENV) $(UV) run python -m modelcheck check --fizz $(FIZZ) \
         --json .model-check/results.json models/admission
     BUILD_LIMITS_MODEL_CHECK=1 $(UV_ENV) $(UV) run pytest -n 0 -rs -m fizzbee tests
     ```

     The pytest end-to-end tests read `results.json`. They do not re-run the
     matrix.
  5. **Documentation.** Add to the developer's guide §11.6 a first "when you
     change `cache_recovery.py` or `admission.py`" checklist, and a
     `make model-check` row in Table 1.
- **Red first.**
  - Add `tests/test_model_check_e2e.py` (marker `fizzbee`), with
    `test_fixed_configuration_holds` and `test_mutants_fail` reading
    `results.json`.
  - Run it before the model exists. Expect it to fail with "configuration
    `head` not found".
- **Acceptance evidence.**
  - EP-M4-E1: `make model-check` reports `holds` on `head` for S1, S2, L1, L3,
    L4a and L4b, each naming its fairness, with every `exists` probe
    satisfied.
  - EP-M4-E2: each mutant yields its predicted counterexample.
  - EP-M4-E3: the `max_actions` fixpoint check holds.
  - EP-M4-E4: `git status --porcelain` is empty afterwards.
  - EP-M4-E5: the gates pass.
  - Roadmap task 1.1.4 is ticked.
- **Conformance check.**
  - 1.1.4 is satisfied.
  - RFC-§1 scope is respected, and D10's refinement is recorded in RFC 0001.
  - The DV1 audit is recorded.
  - No production change.
  - No new trust boundary.
  - Trace links are current.
- **Recovery:** the model files are additive.
- **Remaining gaps:** the known-bad configurations.
- **Compatibility decision:** none.

### EP-M5: incidents 1 and 3, replay and comparator

- **Outcome:** the H1 evidence exists.
- **Requirements and gaps:** roadmap 1.1.5 to 1.1.7; H1; DV2.
- **Steps.**
  1. **Known-bad configurations (1.1.5).**
     - Add two switches:
       - `PREFIX_GUARD_COUNTS_UNCACHED` (I1). It transcribes the stop guard at
         the pre-fix revision found in EP-M1.
       - `PREFIX_TRIGGER_EBA08AD` (I3), as at `eba08ad`:
         - the trigger needs zero visible roots and no cached group moving in
           the same reading, and has no stale proof;
         - `_try_stop` requires the zero-roots proof;
         - `request` is refused while a cached compile runs.
     - Add the configurations:
       - `incident-1-cap3-weak` and `incident-1-cap3-strong` (L3 lasso);
       - `incident-1-cap2-weak` (L3 lasso);
       - `incident-1-cap2-strong-nobackoff` (L3 holds);
       - `incident-1-cap2-strong-backoff` (L3 lasso);
       - `incident-3` (L1 lasso; L4a finite).
     - Red: add them to `configurations.toml` before the switches exist, so the
       materializer rejects the unknown constants.
  2. **Replay (1.1.6).** Add `tests/model_replay.py`. The driver:
     - builds a `harness_for` harness;
     - sets `supervisor.server.clock = lambda: harness.now`;
     - sets `admission.cached` explicitly;
     - maps each model action to its production call. A composite tick
       becomes `admission.group_emptied` for each emptied scope, followed by
       `harness.tick()`. `Submit`, `TokenArrives` and `Cancel` call the
       `Admission` event directly, as the existing tests do.
     - routes `ReturnToken` effects to `FakePool.give_back`;
     - keeps timer arm timestamps, and advances `Harness.now` by
       `arm + duration - now`;
     - projects the production state onto model state after each step.

     Store each counterexample's and witness's normalized trace in
     `models/admission/traces/`, written by `--save-traces` from runs pinned
     to `GOMAXPROCS=1`.

     `tests/test_model_replay.py` has one test per stored trace, plus the
     hold-expiry check. A trace the code follows to a property violation is a
     defect: stop and report it.
  3. **Comparator (1.1.7).** Add
     `tests/test_recovery_progress_comparator.py`.
     - It extends the recovery machine with uncached groups and a
       bounded-progress invariant: once a transaction reaches `stopping` with
       no cached group moving, the stop is issued within N supervisor ticks.
     - N is derived from the retry schedule, and stated in the test.
     - It uses `derandomize=True`, a fixed `max_examples` and a timeout mark.
     - A second test patches the pre-fix guard in and asserts that Hypothesis
       finds a failure. Its examples and wall time are recorded with
       `--hypothesis-show-statistics`.
  4. **Documentation.** Record the switches, the configurations and the
     `transcribes` audit in the RFC's open-questions record.
- **Acceptance evidence.**
  - EP-M5-E1: `make model-check` reports `counterexample as expected` for each
    known-bad expectation, and `holds` for `head`.
  - EP-M5-E2: the capacity-2 results match DV2, or the inversion is reported.
  - EP-M5-E3: every stored trace has a replay test with a disposition.
  - EP-M5-E4: the comparator's cost is recorded beside the model's in
    `Artefacts and notes`.
  - EP-M5-E5: the gates pass.
  - Roadmap tasks 1.1.5 to 1.1.7 are ticked.
- **Conformance check.**
  - 1.1.5 to 1.1.7 are satisfied.
  - The design is followed.
  - DV2's prediction is recorded before the run.
  - No production change: the replay's clock fix is test-side.
  - No new trust boundary.
  - Trace links are current.
- **Recovery:** revert the switches and configurations; the fixed model stands.
- **Remaining gaps:** the verdict.
- **Compatibility decision:** none.

### EP-M6: H1 verdict and G1

- **Outcome:** RFC 0001 carries an "H1 verdict" section containing:
  - the evidence table (configuration, property, predicted, observed, run
    time, states explored, peak memory);
  - the per-property fairness ablation;
  - the comparator comparison;
  - the replay dispositions;
  - the number of model revisions used;
  - a proposed G1 decision: proceed; change tool to TLA+; prefer extending
    Hypothesis; adopt D12's Python explorer; or stop.
- **Requirements and gaps:** roadmap 1.1.8; H1; G1.
- **Acceptance evidence:**
  - EP-M6-E1: the RFC section passes `make markdownlint`.
  - Roadmap task 1.1.8 is ticked when the user's decision is recorded.
- **Conformance check.**
  - Every Table 4 W1 row has an observed result.
  - No unrecorded deviation remains.
  - Trace links are current.
- **Stop.** Set this plan's status to `BLOCKED` pending the user's G1
  decision, and record it in `Decision log`.
- **Recovery:** not applicable.
- **Remaining gaps:** W2 to W4.
- **Compatibility decision:** none.

### Later milestones (expanded only after each gate)

These outlines fix scope and trace links. Each is expanded to full template
fields, with red tests and acceptance evidence, when its gate opens.

- **EP-M7: full environment and safety** (roadmap 1.2.1, 1.2.2).
  - Adds signals and clean exits, bounded cancellations, detached server
    work, the user manager with transport faults, the operator, health, the
    barrier and ordered timer units.
  - Adds S3 to S9.
- **EP-M8: liveness, timers and known-bad configurations** (roadmap 1.2.3 to
  1.2.5, 1.3.1, 1.3.2).
  - Adds L2, with `TokenArrives` strongly fair under F5.
  - Adds R1 in timer units, `incident-2` (finite counterexamples to S8 and
    R1) and `incident-4` (a finite counterexample to S9).
  - Adds `stale-path-37d08e5` (L1 holds with F3, fails without it; L4b holds
    without F3), and the HEAD grace under F3a.
  - Every new counterexample is replayed.
- **EP-M9: bounds, start-up and G2** (roadmap 1.2.6, 1.3.3, 1.3.4).
  - Times H2's bounds: capacity 3, two waiting requests, two abnormal endings,
    one manager fault and a restart limit of 2.
  - Settles OQ12. If start-up is in scope, a start-up action cites the merged
    `cache_quiesce.py` path, and S3, S4 and L1 hold across a restart that
    finds a live cache. Otherwise the deferral is recorded in roadmap 2.1.3.
  - Records W2's exit evidence and the H2 verdict, and stops for G2.
- **EP-M10: drift and regressions** (roadmap 1.4.1, 1.4.2).
  - Adds `tests/test_model_drift.py` over the real inventory (public
    `Admission` event methods, `Effect` members, `Phase` and `FaultKind`
    literals), with reasoned exclusions, such as `begin_closing` while the
    closing path is out of scope.
  - Verifies each `transcribes` SHA.
  - Adds a regression or correspondence test for every full-model
    counterexample, and records the OQ7 answer in RFC 0001.
- **EP-M11: projection and G3** (roadmap 1.4.3, 1.4.4).
  - Adds `tests/test_model_projection.py` (T7).
  - Records the H3 verdict and the G3 proposal, and stops.
- **EP-M12: gate and CI** (roadmap 1.5.1, 1.5.2).
  - Finalizes `make model-check` with `MODEL_BOUNDS=routine|nightly`, and
    checks that two runs give identical verdicts and state counts.
  - Adds `.github/workflows/model-check.yml`:
    - **Triggers:**
      - pull requests touching `models/` or `modelcheck/`;
      - pull requests touching any of the eleven update-policy modules:
        `admission`, `admission_state`, `effects`, `groups`, `group_drain`,
        `server_watch`, `sccache`, `cache_recovery`, `recovery_starvation`,
        `recovery_barrier` and `supervisor_polling`;
      - nightly, in a cron slot outside the 03:05 to 09:30 UTC band other
        workflows use.
    - **Caching:** the tarball is cached under the key
      `fizzbee-<version>-<sha256>-linux-x64`, with no restore keys, and its
      digest is re-checked after every restore.
    - **Run limits:** `concurrency` with `cancel-in-progress: false`, and
      `timeout-minutes: 75`.
    - **Artefacts:** the report, traces and memory log are uploaded.
    - **Failure handling:** on a nightly failure, one tracking issue is opened
      or updated.
    - **Required-check readiness:** a single always-running job whose steps
      do nothing when no relevant path changed, so the job can be made
      required.
- **EP-M13: documentation and G4** (roadmap 1.5.3).
  - Completes the developer's guide §11.6: the model, the update policy and
    the version-bump procedure, which re-captures the EP-M1 fixtures.
  - Adds a §13 CI bullet.
  - Records the G4 decision on joining `make all`, and on making the job
    required (OQ10).

## Concrete steps

All commands run from the repository root, the worktree
`/home/leynos/.lody/repos/github---leynos---buildlimits/worktrees/3b0cd6e0-5780-46a7-a5d4-6d223b4e16e6`,
or the worktree that replaces it. Gate logs go to `/tmp`, named
`/tmp/$ACTION-buildlimits-$(git branch --show-current).out`.

Install and verify the checker, then run the spike (EP-M1):

```bash
make fizzbee 2>&1 | tee /tmp/fizzbee-buildlimits-$(git branch --show-current).out
BUILD_LIMITS_MODEL_CHECK=1 uv run pytest -n 0 -rs -m fizzbee \
  tests/test_fizzbee_spike.py tests/test_fizzbee_graphs.py \
  tests/test_fizzbee_fixtures.py \
  2>&1 | tee /tmp/spike-buildlimits-$(git branch --show-current).out
git status --porcelain
```

Expected: the second command ends `N passed` with no skips, and the third
prints nothing.

Run the gates after each milestone. The `scrutineer` agent runs them in
sequence:

```bash
make check-fmt 2>&1 | tee /tmp/check-fmt-buildlimits-$(git branch --show-current).out
make typecheck 2>&1 | tee /tmp/typecheck-buildlimits-$(git branch --show-current).out
make lint 2>&1 | tee /tmp/lint-buildlimits-$(git branch --show-current).out
make test 2>&1 | tee /tmp/test-buildlimits-$(git branch --show-current).out
make markdownlint 2>&1 | tee /tmp/markdownlint-buildlimits-$(git branch --show-current).out
make nixie 2>&1 | tee /tmp/nixie-buildlimits-$(git branch --show-current).out
make model-check 2>&1 | tee /tmp/model-check-buildlimits-$(git branch --show-current).out
```

For screen readers: the following block shows the expected shape of the
model-check report from EP-M5 onwards. There is one line per expectation,
followed by a summary line. The exact wording is pinned by the Syrupy snapshot.

```plaintext
head                              S1   holds                        states 41210  fairness: none
head                              L3   holds                        states 41210  fairness: F1, F2, F5, F6
incident-1-cap3-weak              L3   counterexample as expected   lasso, 9 steps  fairness: F1(weak), F2, F5, F6
incident-1-cap2-strong-nobackoff  L3   holds                        states 18034  fairness: F1(strong), F2, F5, F6
incident-3                        L4a  counterexample as expected   finite, 6 steps
model check: 23 expectations met, 0 unmet
```

Commit after each green step, once the gates pass, with a message in the
imperative mood. Push to the draft pull request.

## Validation and acceptance

Quality criteria:

- **Tests:** `make test` passes, including the behavioural feature, the T1 to
  T6 and T8 tests, the command-line end-to-end and process-control tests, and
  the report snapshots. `make model-check` passes, with no `fizzbee` test
  skipped.
- **Verification:** M-S, M-L and M-KB are discharged for W1's scope at G1,
  and for the full scope at G2. T1 to T6 and T8 are discharged in EP-M2 to
  EP-M5, and T7 at G3.
- **Lint and typecheck:** `make check-fmt`, `make lint` and `make typecheck`
  exit 0.
- **Documentation:** `make markdownlint` and `make nixie` exit 0.
- **Performance:** routine bounds finish in under 10 minutes and nightly
  bounds in under 60 minutes, on the development host and on a hosted runner
  (G2, G4). No single run exceeds 8 GiB.
- **Security:**
  - the binary is fetched over HTTPS from the pinned release, and its digest
    is checked before unpacking;
  - unpacking rejects unsafe paths;
  - no shell strings are built from input;
  - process groups are always reaped.

Quality method:

- the `scrutineer` agent runs the gate commands in `Concrete steps`, in
  sequence, and reports the log paths;
- `git status --porcelain` is checked after every `make model-check`;
- from EP-M12, the CI job runs `make model-check` on a hosted runner.

Red-Green-Refactor evidence is recorded in `Artefacts and notes` for each
milestone:

- the red command and its expected `XFAIL` or failure reason;
- the green command;
- the refactor gate run.

## Idempotence and recovery

- **`make fizzbee`** is idempotent and atomic:
  - a stamp file marks a verified install;
  - a partial download leaves only a `.part` file or a temporary directory,
    which the next run replaces;
  - a lock prevents two installs racing.
- **Checker runs** write only under `.model-check/` and `$(FIZZBEE_HOME)`.
  Deleting `.model-check/` is always safe.
- **Milestones** are additive, so recovery is reverting the milestone's
  commits.
- **Trace files** in `models/admission/traces/` are regenerated by
  `--save-traces`, and reviewed as diffs.

## Artefacts and notes

Filled in during implementation: the red and green transcripts, the pre-fix
revision table, the toy-model answers, the determinism results, the state
counts, run times and peak memory, the comparator's cost, and the replay
dispositions.

## Interfaces and dependencies

There are no new Python dependencies. FizzBee v0.5.4 (Apache-2.0) is an
external tool, pinned in the `Makefile` by version and digest. It needs bash,
the GNU coreutils the launcher calls, and a `python3` on `PATH`; it needs no
network, no container runtime and no JVM. RFC 0001 open question 4 records the
release census and the dependencies, and open question 5 the licence and the
bundled notices.

For screen readers: the following block is a directory tree of the planned
`modelcheck` package and the `models` directory. Each entry names the module's
responsibility.

```plaintext
modelcheck/
├── __init__.py
├── __main__.py              wiring only: builds adapters and calls cli.make_app
├── cli.py                   driving adapter: Cyclopts app factory, exit codes
├── domain.py                Configuration, expectation kinds, result union, Verdict, MaterializedSpec
├── expectations.py          judge(): expectation x result -> Judgement
├── materialize.py           materialize(): source + configuration -> MaterializedSpec
├── drift.py                 parse_mappings(), drift() -> DriftReport
├── trace.py                 Trace, Step, cycle and stutter markers
├── report.py                render_report(), render_drift()
├── ports.py                 ModelChecker, CodeInventory, ConfigurationSource
├── service.py               run_matrix(): the application service over the ports
└── adapters/
    ├── fizzbee_output.py    parse_output(), normalize_trace(): pure translation of FizzBee output
    ├── fizzbee_process.py   FizzbeeChecker: scratch directory, Popen, process-group timeout
    ├── configuration.py     TomlConfigurations: tomllib over configurations.toml
    └── inventory.py         BuildlimitsInventory: importlib and typing.get_args over buildlimits
models/
├── spike/                   EP-M1 toy models, kept as language checks
└── admission/
    ├── admission.fizz       the model
    ├── fizz.yaml            default bounds and options
    ├── configurations.toml  fixed, mutant and known-bad configurations with expectations
    └── traces/              normalized counterexample and witness traces
```

_Figure 2: Planned layout of the tooling and models._

The ports, in `modelcheck/ports.py`, follow the house Protocol style: a
docstring as the body, with no `...`.

```python
class ModelChecker(typ.Protocol):
    """Run one materialized specification and classify the outcome."""

    def check(self, spec: MaterializedSpec) -> CheckResult:
        """Return the classified result; the adapter owns its scratch directory."""


class CodeInventory(typ.Protocol):
    """Enumerate the production surface the model must map."""

    def items(self) -> frozenset[str]:
        """Return dotted paths of events, effects, phases and fault kinds."""

    def resolves(self, dotted_path: str) -> bool:
        """Report whether a mapped dotted path still names something."""


class ConfigurationSource(typ.Protocol):
    """Supply the model source, its configurations and drift exclusions."""

    def source_text(self) -> str:
        """Return the concatenated model source."""

    def configurations(self) -> tuple[Configuration, ...]:
        """Return every configuration with its expectations."""

    def exclusions(self) -> cabc.Mapping[str, str]:
        """Return inventory items deliberately unmapped, each with its reason."""
```

The domain contracts, in `modelcheck/domain.py`:

```python
class CounterexampleKind(enum.StrEnum):
    FINITE = "finite"
    LASSO = "lasso"
    STUTTER = "stutter"
    DEADLOCK = "deadlock"


@dc.dataclass(frozen=True, slots=True)
class Passed:
    states: int
    elapsed_seconds: float
    unsatisfied_probes: tuple[str, ...]


@dc.dataclass(frozen=True, slots=True)
class Counterexample:
    property_name: str
    kind: CounterexampleKind
    trace: Trace
    states: int
    elapsed_seconds: float


@dc.dataclass(frozen=True, slots=True)
class Stopped:
    detail: str


@dc.dataclass(frozen=True, slots=True)
class Errored:
    detail: str


type CheckResult = Passed | Counterexample | Stopped | Errored


@dc.dataclass(frozen=True, slots=True)
class Holds:
    property_name: str


@dc.dataclass(frozen=True, slots=True)
class FailsAs:
    property_name: str
    kind: CounterexampleKind


@dc.dataclass(frozen=True, slots=True)
class Reachable:
    probe_name: str


type Expectation = Holds | FailsAs | Reachable


@dc.dataclass(frozen=True, slots=True)
class Configuration:
    name: str
    constants: cabc.Mapping[str, str]
    fairness: cabc.Mapping[str, str]          # tag -> "weak" | "strong" | "unfair"
    options: cabc.Mapping[str, object]        # complete frontmatter
    min_states: int
    expectations: tuple[Expectation, ...]


@dc.dataclass(frozen=True, slots=True)
class MaterializedSpec:
    configuration: str
    text: str
    fairness_in_force: tuple[str, ...]


class Verdict(enum.StrEnum):
    HOLDS = "holds"
    AS_EXPECTED = "counterexample as expected"
    REACHED = "reached"
    MISSING = "expected counterexample missing"
    WRONG_KIND = "wrong counterexample kind"
    UNEXPECTED = "unexpected counterexample"
    UNREACHABLE = "unreachable"
    BELOW_FLOOR = "state space below floor"
    STOPPED = "stopped"
    ERROR = "error"


@dc.dataclass(frozen=True, slots=True)
class Judgement:
    configuration: str
    expectation: Expectation
    result: CheckResult
    verdict: Verdict
    fairness_in_force: tuple[str, ...]

    @property
    def is_met(self) -> bool:
        """Report whether the verdict satisfies the expectation."""
        return self.verdict in {Verdict.HOLDS, Verdict.AS_EXPECTED, Verdict.REACHED}
```

The functions:

```python
def judge(configuration: Configuration, expectation: Expectation,
          spec: MaterializedSpec, result: CheckResult) -> Judgement: ...
def materialize(source: str, configuration: Configuration,
                *, expected_failure: str | None) -> MaterializedSpec: ...
def drift(mapped: frozenset[str], inventory: frozenset[str], resolves: cabc.Callable[[str], bool],
          exclusions: cabc.Mapping[str, str]) -> DriftReport: ...
def run_matrix(source: ConfigurationSource, checker: ModelChecker) -> tuple[Judgement, ...]: ...
def parse_output(stdout: str, exit_status: int, error_graph: str | None) -> CheckResult: ...
```

Hexagonal boundaries:

- `domain`, `expectations`, `materialize`, `drift`, `trace` and `report` are
  pure.
- `service` depends on `ports` and the domain only, and takes a `drift`
  inventory as values rather than calling `buildlimits`.
- `adapters/*` translate between the outside world and the domain.
  `fizzbee_output` is pure translation; the other adapters perform input,
  output or process control.
- `cli.make_app` receives its adapters as factories, and `__main__` is the
  only place that constructs them.
- `tests/test_modelcheck_layering.py` enforces these rules (T8).

## Revision note

- 2026-10-24: first draft, from Wyvern reconnaissance and FizzBee research.
- 2026-10-24: revised after a four-panellist community-of-experts review.
  - **Changed: model abstraction.**
    - The baseline move became deviation DV1.
    - DV2 refined the capacity-2 prediction for the stop's backoff.
    - DV3 split L4 and added F3a.
    - D10 introduced the composite supervisor tick.
    - Leader exits became per-class fair actions.
  - **Changed: result types.** The result type became a tagged union with
    stutter and deadlock kinds, reachability probes and a `Verdict`
    enumeration.
  - **Changed: anti-vacuity measures.** These were added: retained
    assertions, equal state counts, `min_states` floors, the `max_actions`
    fixpoint and truncation fixtures.
  - **Changed: operations and supply chain.**
    - Checker runs moved to scratch directories under `.model-check/`.
    - The binary moved to a shared content-addressed cache, with a hardened
      install.
    - Real-binary tests became opt-in, with skips forbidden under
      `make model-check`.
    - Process groups are killed on timeout.
    - Resource limits were added.
  - **Changed: replay.** The replay driver now fixes the `ServerWatch`
    clock.
  - **Changed: milestone structure.**
    - The `modelcheck` milestone was split into EP-M2 (domain) and EP-M3
      (adapters), and later milestones were renumbered.
    - Template fields were added to every W1 milestone.
    - Documentation now lands with each milestone.
    - Every roadmap task is traced individually.
    - The skills and documents to read are signposted.
  - **Effect on remaining work:** approval now also covers DV1 to DV3.
    EP-M1 to EP-M6 are executable as written, and later milestones are
    expanded at their gates.
