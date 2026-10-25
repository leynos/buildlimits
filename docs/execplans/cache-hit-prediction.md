# Record the real outcome of each cached compile and evaluate prediction offline

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: APPROVED FOR EXECUTION; IN PROGRESS (2026-10-25; EP-M0 is complete; r31
documentation gates and review passed; EP-M1 validation, audit evidence and
documentation gates and milestone review passed; EP-M1 commits are complete;
EP-M2 is complete in `327ae5a` and `080d9c0`, with final gates and local
CodeRabbit review passed; EP-M3 red contracts are committed as `195ff77` and
its implementation and full gates passed; EP-M4 implementation, full gates and
clean review passed in `636f3ed`; EP-M5 child and query adapters are committed
as `d96518c` and `56bc46b`; the store writer is committed as `34ea33b`, with
candidate98 full gates and archive-bound review passed)

Branch: `cache-hit-prediction`. Plan path:
`docs/execplans/cache-hit-prediction.md`. Upstream: RFC 0002, accepted
2026-10-23 at `42f40e4`.

## Purpose / big picture

Nothing in buildlimits knows whether a compile was an sccache hit. A waiting
request in `build-limits-status` could be a 200-millisecond cache read or a
five-minute compile, so nobody can judge proposals to admit hits sooner without
knowing how often hits occur and whether they can be foreseen.

RFC 0002 answers this in layers, each gated on evidence from the one before.
Its acceptance approves workstreams W1 and W2 only:

- W1, recording: the supervisor reads the managed sccache server's global
  statistics at _quiet points_, when no cached compile is moving and nothing
  waits. Wherever the counters can say which request a change belongs to, it
  labels the cached compile `hit`, `miss` and so on. The label is journalled as
  a closed-vocabulary metric, and a keyed record is appended to a local store
  for offline analysis. While cached compiles run, the supervisor also looks
  for each request's full-emit compiler among the server's processes (a _child
  sighting_), so that H2 can test whether wall time and sightings separate hits
  from misses where the counters cannot. A real-binary check confirms the
  sccache facts both rely on.
- W2, offline evaluation: a replay over W1's records yields the H3 to H5
  verdicts (whether a hit can be foreseen from history) without building any
  prediction into admission.

Layers 2 and 3 (W3 to W6) are out of scope. They proceed only when the owner's
gates G2 to G5 release them, and each needs its own plan.

After W1, an operator can set `cache_outcomes = "record"` in
`~/.config/buildlimits/supervisor.toml` and build as normal. They will then see:

- a `cache outcomes: recording (812 attributed, last reading none)` line in
  `build-limits-status`;
- a `cache_outcomes` section in
  `build-limits-metrics --since 1h --max-records 100000` that counts results,
  reasons and coverage;
- the H2 and H3 verdicts from the local store, by running
  `build-limits-cache-replay h2` and `build-limits-cache-replay h3`.

After W2, which starts only once the owner has decided G1,
`build-limits-cache-replay` also reports H4 and H5 against thresholds that were
fixed before any data was read.

Admission, token accounting, `SERVER_DRAIN` retention and cache recovery behave
identically whether recording is on or off. That is the central safety claim,
and a differential test proves it.

## Constraints

These are hard invariants. Violating one requires escalation, not a workaround.

- C-OBS (RFC I1, observation only): no decision in the modules below reads an
  outcome, a record, a key or a reading. A transitive import-closure test
  enforces this. The modules are `buildlimits/admission.py`,
  `admission_state.py`, `effects.py`, `server_watch.py`, `cache_recovery.py`,
  `recovery_starvation.py`, `recovery_roots.py` and `recovery_outlook.py`.
- C-NOSTART (RFC I2): the supervisor runs only
  `sccache --show-stats --stats-format json`, never `--zero-stats`,
  `--dist-status` or `--start-server`. It never queries under any of these
  conditions:
  - a recovery hold is active;
  - the restart barrier exists;
  - the cache is unreachable;
  - recovery is not idle.
- C-ISOLATE (RFC I3): a statistics, generation, store or salt failure yields
  a named reason. It never makes the supervisor unhealthy, never refuses a
  request and never delays an admission. Every code path this plan adds to the
  selector loop must be non-blocking and _total_, meaning it catches its own
  exceptions. `DrainLoop._failure` calls `fail_health` on the first escaped
  exception (S6).
- C-TOKEN: no hook this plan adds may sit between `Groups.remove` and
  `Admission.group_emptied`, or before `Groups.release`. A failing hook must
  never strand a token or delay its return.
- C-NOINFER (RFC I4): an attributed label is never inferred. W1 produces only
  `solo` and `unanimous` attributions. `inferred` is a registered vocabulary
  token that W1 never emits.
- C-PRIV (RFC §2 privacy): no raw path, argument, environment value or
  toolchain name is written to the journal, a span or the store. Any value that
  can carry a local path is keyed with the per-host salt. This covers the
  manifest directory, the compiler as given, `RUSTUP_TOOLCHAIN` and the
  argument vector. No salt means no record. Records never leave the host.
- C-DEFAULT: every new behaviour sits behind `cache_outcomes`, which
  defaults to `off`. With `off` the supervisor builds a null shell: it spawns
  no new process, opens no new file or socket, starts no thread and emits no
  new event.
- C-SIZE: no code file exceeds 400 lines (AGENTS.md). `supervisor.py` (392
  lines), `server_watch.py` (392), `cache_recovery.py` (400) and `groups.py`
  (391) have no headroom. New logic lives in new modules, and existing files
  gain at most a handful of lines.
- C-HOST:
  - Never stop, restart, signal or reconfigure the live
    `buildlimits-supervisor.service` or `buildlimits-sccache.service`.
  - Never install on the host or run `build-limits-recover`.
  - Never bypass compile admission: no test or tool runs a real `rustc`
    outside an admitted scope.
  - Read-only journal queries and a read-only `--show-stats` against the
    managed socket are permitted (A5).
- C-PY: the Python 3.14 baseline and the tools pinned in `pyproject.toml` and
  `Makefile`. No new runtime or development dependency. The standard library's
  `hashlib`, `hmac`, `secrets`, `json`, `socket`, `subprocess`, `threading` and
  `queue` suffice.
- C-SCHEMA: `cache_outcome`, `outcome_reading` and `outcome_store` enter
  `metrics_schema.EVENTS` in the same release that can emit them, or an earlier
  one (RFC "Compatibility and migration").
- C-GATES: W2 implementation (EP-M9) must not begin until the owner records
  the G1 decision (`proceed` or `narrow`) in `Decision log`. W3 and later are
  out of scope.

## Tolerances (exception triggers)

- Scope: escalate if W1 needs more than 32 changed or new production source
  files, or more than 3,200 net added lines of production code.
- Interface: escalate before changing any of the following:
  - the wire protocol (`protocol.py`);
  - any existing status-document key;
  - the effect types in `effects.py`;
  - any existing metric event's fields.

  Adding the `cache_outcomes` status object is within tolerance.
- Admission: escalate if any milestone appears to need a change to
  `admission.py` or `admission_state.py`.
- Dependencies: escalate if a new dependency seems necessary.
- Iterations: escalate if a gate still fails after three fix attempts on the
  same cause.
- Feasibility: escalate if EP-M1 is still no-go after a 1.5-hour time-box.
  The fallback is in D9.
- Coverage evidence: the G0 measurement below projects the solo-window bound
  on this host at 4.5%, under H1's 5% falsification line. The owner answered Q1
  with (b): W1 also collects child sightings for H2 (EP-M8). Escalate if a
  re-run of G0 at the start of EP-M1 falls below 2%.
- Performance: store I/O runs on a writer thread, so the loop never waits on
  a file. Escalate if the opt-in latency test shows either of these:
  - p99 tick lengthening above 5 ms while a reading is in flight;
  - p99 append enqueue above 1 ms;
  - p99 child-sighting sample above 2 ms, or more than 64 processes inspected
    in one sample.
- Ambiguity: stop and present options if EP-M1 contradicts axiom A2 or A3.

## Risks

- R1. Statistics may trail the reply. The installed binary may not order
  statistics and reply as its 0.14.0 source reads (A2), and a partial footprint
  would read as a wrong label.
  - Severity: high.
  - Likelihood: low, because the source shows one guard taken before the
    reply (S1).
  - Mitigation: EP-M1 reads the counters immediately after each of 200
    client exits. Any partial footprint activates D12.
- R2. Coverage is structurally low on a busy host.
  - Severity: medium.
  - Likelihood: high. Evidence G0 (S5) puts the solo-window bound at 4.5% of
    cached finishes, about 3,000 a day here.
  - Mitigation:
    - The absolute count clears the 2,000-outcome minimum within a day.
    - The gate clause `queued == 0` stops wasted readings that would
      straddle a launch.
    - Every verdict states the quiet-window bias.
    - The owner chose (Q1 (b)) to collect child sightings in W1 (EP-M8), so
      that G1 can judge H2, the RFC's route to coverage below 5%.
- R3. Failure loops could flood the journal, at about 86,400 events a day
  with a 1-second retry.
  - Severity: medium.
  - Likelihood: medium.
  - Mitigation:
    - Exponential back-off from 1 s to 60 s.
    - A transition-only `outcome_reading` event, plus a once-a-minute
      aggregate.
    - Recording disables itself until restart after `unsupported_version`
      (D16).
- R4. Store I/O could stall the loop under heavy write-back.
  - Severity: high if it happens.
  - Likelihood: medium on a build host.
  - Mitigation: append-only JSON Lines owned by a single writer thread. The
    loop only enqueues into a bounded queue, compaction runs on the thread,
    and the stop path joins the thread with a 1-second bound after tokens
    are settled (D17).
- R5. A downgrade could stop start-up. If the package is downgraded with
  `cache_outcomes` still in `supervisor.toml`, start-up exits 2, and
  `RestartPreventExitStatus=2` keeps the service down.
  - Severity: high.
  - Likelihood: low.
  - Mitigation:
    - EP-M6 is a releasable, inert plateau that accepts the key, so a
      downgrade to it is safe.
    - The users' guide says to remove the key before downgrading further.
    - The plan recommends that the dev-env-rocky role render the key only
      when it is not `off` (D18).
- R6. The fake toolchain may not pass sccache's Rust detection.
  - Severity: medium.
  - Likelihood: medium.
  - Mitigation: EP-M1 is a 1.5-hour time-boxed prototype that does not block
    EP-M2 to EP-M7, and D9 is its fallback.
- R7. Readers that predate the release drop the new events whole (RFC open
  question 6).
  - Severity: low.
  - Mitigation: recording is off by default, and the upgrade order is
    documented.
- R8. The 10,000-record bound, not 30-day ageing, sets how much history W2
  sees: about 1 to 3 days at this host's rate.
  - Severity: low.
  - Mitigation:
    - ADR 004 records the limit.
    - The replay reports the time span it covers.
    - If H4 or H5 is `insufficient`, W2 may propose per-key retention as a
      separate decision.
- R9. Child sightings miss short misses and may cost loop time. A sample every
  `CHILD_SAMPLE_SECONDS = 0.25` cannot see a full-emit child that lives less
  than that, and reading `/proc/<pid>/cmdline` takes the target's `mmap_lock`,
  so it can stall briefly while that process faults heavily.
  - Severity: medium (it biases H2 towards long misses; a stall delays one
    tick).
  - Likelihood: medium for short misses, low for stalls.
  - Mitigation:
    - Each record stores `child_samples`, so H2 can be computed over groups
      that were sampled at least once and the bias is visible.
    - Sampling runs only while `cached_moving > 0`, inspects at most 64
      processes, and reads each process's `cmdline` once per `(pid,
      starttime)`.
    - The opt-in latency test measures the sample cost against the
      performance tolerance.
    - H2's falsification ends sampling (G1 runbook).

- R10. An append can leave a partial tail if both the write and its rollback
  fail.
  - Severity: high if a later row makes the fragment a non-final line.
  - Likelihood: low; both the write and `ftruncate` must fail.
  - Mitigation: a failed append schedules compaction from the validated
    in-memory records before another append is accepted. If that compaction
    also fails, the later row is discarded and the failure is reported. The
    injected write-and-rollback failure is covered by O8.

## Progress

- [x] (2026-10-23 19:05Z) Branch renamed to `cache-hit-prediction`; RFC 0002
  read in full.
- [x] (2026-10-23 19:20Z) Wyvern reconnaissance by four agents, covering
  admission, the loop, `ServerWatch` and `ManagedSccache`, metrics, tracing,
  status, tests and the documentation conventions.
- [x] (2026-10-23 19:25Z) Firecrawl checks of sccache 0.14.0 `ServerStats`,
  `ServerInfo`, `PerLanguageCount` and `CACHE_VERSION`, and of systemd
  `InvocationID`.
- [x] (2026-10-23 19:50Z) A2 and A3 settled from the 0.14.0 source (S1).
- [x] (2026-10-23 20:05Z) First draft committed (`ef248d5`).
- [x] (2026-10-23 21:10Z) The Logisphere expert panel of six agents
  reviewed the draft. Verdict: proceed with conditions. The synthesis is in
  `Decision log` D15 to D23 and in `Outcomes & retrospective`.
- [x] (2026-10-23 21:20Z) G0 coverage bound measured read-only from this
  host's journal (S5).
- [x] (2026-10-23 21:40Z) Plan revised for every 🔴 and 🟡 panel finding.
- [x] (2026-10-23 22:30Z) PR #24 opened as a draft. Issue #25 raised for the
  unrelated `ty` version drift on `main`.
- [x] (2026-10-23 23:00Z) The owner answered Q1 with (b). EP-M8 (H2 child
  sightings) was added to W1, and W2 was renumbered EP-M9 (D24, D25).
- [x] (2026-10-23 23:27Z) Owner approved the whole plan, including deviations
  D2 to D25. EP-M0 is authorized to begin.
- [x] (2026-10-24 00:10Z) EP-M0 started after plan approval commit `0400924`.
- [x] (2026-10-24 00:18Z) Committed ADR 004's decisions, H1-H5 thresholds and
  H2 classifier as `ddfaeae` before one read-only stats query. Captured
  `tests/fixtures/sccache_0_14_0_stats.json` from the managed socket and
  redacted `cache_location` and `basedirs`. G0 remains recorded in S5.
- [x] (2026-10-24 00:26Z) Final documentation gates passed sequentially at
  `ddfaeae` plus the fixture and evidence changes. Local CodeRabbit milestone
  review remains pending.
- [x] (2026-10-24 01:02Z) D29 review corrections passed `make fmt`,
  `make check-fmt`, `make markdownlint` and serial `make nixie` in the isolated
  container, then were committed as `6d2589c`.
- [x] (2026-10-24 01:08Z) Fresh CodeRabbit review of `6d2589c` completed with
  two major and one minor finding. Source and design review confirmed missing
  H3 parser branches and shared-metadata ambiguity; D30-D32 specify the fixes.
- [x] (2026-10-24 01:17Z) D30-D32 passed `make fmt`, `make check-fmt`,
  `make markdownlint` and serial `make nixie` in the isolated container.
  Formatting output was copied into the worktree; a fresh exact-head review is
  the remaining EP-M0 gate.
- [x] (2026-10-24 01:39Z) Fresh CodeRabbit review of `278cccb`
  completed with nine findings. D33 and the linked RFC/ADR corrections
  reconcile the HMAC, store, event-field, parser and toolchain contracts;
  documentation gates and a fresh review were pending at that point.
- [x] (2026-10-24 02:02Z) Isolated `make fmt` and `make check-fmt` passed.
  The first `make lint` run found 68 issues in EP-M1's new Python test support;
  later gates had not yet run. D34 separated pure statistics fixture handling
  into `tests/sccache_stats.py`; subsequent candidate gates passed.
- [x] (2026-10-24) The rebuilt isolated candidate passed `make fmt`,
  `make check-fmt`, `make lint` and `make typecheck`. The full suite passed at
  `PYTEST_XDIST_WORKERS=1` (1,763 passed, 26 skipped), and the opt-in
  `test_real_sccache_014_counter_and_child_contracts` passed against the real
  sccache 0.14.0 server (1 passed). Its 204-line JSONL fixture was extracted
  only after the no-mount container exited successfully; SHA-256:
  `14a9490fe20f356789a9707eca1f63b189fc8f56be9a0a21ffcf3346de376058`.
- [x] (2026-10-24) Fresh CodeRabbit review of `9098857` completed with six
  findings: two reports on the D33 field inventory, two on historical progress
  wording, one on the RFC salt operation, and one grammar correction. The
  inventory omissions and salt-operation wording are corrected below; the
  earlier progress states are now explicitly historical.
- [x] (2026-10-24) Fresh CodeRabbit review of `684fffde` completed with six
  findings on en-GB spelling, grammar, 80-column wrapping, the 12 MiB trigger
  wording and the store-port contract. The follow-up matches the RFC to
  `OutcomeStorePort` and keeps load and compaction inside `StoreWriter`.
- [x] (2026-10-24) Fresh CodeRabbit review of `8e0bd40` completed with seven
  low findings: invalid illustrative JSON, reading-reason vocabulary, bounded
  close wording, shared-store replay fields, separate toolchain HMAC fields and
  RFC punctuation. The ADR, RFC, and plan now align on those contracts; final
  docs gates and a fresh exact-head review are pending.
- [x] (2026-10-24) The previous Markdown overlay passed `make fmt`,
  `make check-fmt`, `make markdownlint` and `make nixie`. Fresh local
  CodeRabbit review of the docs overlay against `278cccb` completed with zero
  findings. The reviewed files were ADR 004, the ExecPlan, RFC 0002 and the
  developers' guide; the source candidate was `8e0bd40`. The later r6
  corrections below require the final docs gates and review to run again.
- [x] (2026-10-24) Fresh overlay CodeRabbit review against `278cccb` through
  source candidate `8e0bd40` reported two valid RFC findings: the port
  description still used the rejected unit-properties contract, and the record
  inventory called its retention field `timestamp`. The RFC now uses the
  accepted `ServerProcessPort` D2/D24 contract and names `observed_at`; final
  docs gates and fresh review are pending.
- [x] (2026-10-24) Fresh overlay review against `278cccb` through `8e0bd40`
  reported three valid prose findings: use `countermovement`, state the fixed
  30-day retention without a proposal qualifier, and define `read_error` as a
  non-timeout reading-operation failure with socket and transport-read
  examples. The RFC and ExecPlan now match; D13 order is unchanged. Final docs
  gates and fresh review are pending.
- [x] (2026-10-24) Fresh review of the four-doc overlay found four valid
  vocabulary findings: pin the helper inventory to sccache 0.14.0, list all
  D13-D16 `outcome_reason` values, reserve `no_quiet_point` for aggregate
  `readings_skipped`, and remove it from the `OutcomeReason` type. The RFC,
  developers' guide and ExecPlan now reflect those corrections; final docs
  gates and fresh review are pending.
- [x] (2026-10-24) The r15 docs-only run stopped at `make fmt` because it
  reported formatter output in the RFC and developers' guide. Applied that
  formatter diff; final docs gates and fresh review remain pending.
- [x] (2026-10-24) The r17 docs-only run stopped after `make fmt` reported one
  remaining wrap in the status paragraph. Applied that formatter correction;
  final docs gates and fresh review remain pending.
- [x] (2026-10-24) Fresh r19 review found four cross-document mismatches:
  `evaluation_reason` was missing from the inventory and O6, store reasons
  omitted accepted O8 outcomes, reading-failure wording excluded query launch,
  and tracing operations omitted store compaction and `admission_selection`.
  `StatisticsPort.start` returns `Ticket | QueryFailure`, so a failed query
  launch is part of the reading operation and belongs to `read_error`, after
  the higher-priority timeout check. The RFC, ADR, and plan now agree on that
  rule and on O6/O8/D13-D16 and EP-M6/M7/M9; final docs gates and fresh review
  remain pending.
- [x] (2026-10-24) The r27 review found two minor punctuation issues. Added
  Oxford commas to three-item lists in the RFC and plan, including the
  reading-failure examples and historical review summaries.
- [x] (2026-10-24) The r21 documentation run stopped at `make fmt` due to
  formatter output in the RFC and ExecPlan. Applied the exact formatter diff;
  final documentation gates and fresh review remain pending.
- [x] (2026-10-24) The corrected four-doc overlay passed `make fmt`,
  `make check-fmt`, `make markdownlint` and `make nixie` sequentially with an
  empty formatter diff. A fresh CodeRabbit review against `278cccb` reported
  zero findings across ADR 004, the ExecPlan, RFC 0002 and the developers'
  guide at source candidate `8e0bd40`. The r19 contract and r21 formatter
  corrections are included.
- [x] (2026-10-24) After the r27 Oxford comma fixes, the four-doc overlay passed
  `make fmt`, `make check-fmt`, `make markdownlint` and `make nixie`
  sequentially with an empty formatter diff. CodeRabbit 0.9.1 reviewed the four
  documents against `278cccb` through `8e0bd40` and found zero issues.
- [x] (2026-10-24 10:29Z) Confirmed D26's approval remains in effect and marked
  the plan approved for execution before resuming implementation.
- [x] (2026-10-24 11:56Z) EP-M1's current candidate passed isolated
  `make check-fmt`, `make typecheck`, `make lint` and serial `make test` (1,776
  passed, 26 skipped, 97 warnings). The opt-in real sccache 0.14.0 acceptance
  check passed (1 passed). The locked development dependency set passed a
  separate metadata-only `pip-audit` scan with no known vulnerabilities.
  `make audit` itself could not reach PyPI from the required networkless
  candidate sandbox; no lockfile change was needed.
- [x] (2026-10-24 11:56Z) The first isolated test run exposed a temporary
  environment issue in a nested `uv run --no-project` call; rerunning with the
  seeded project-local cache addressed it. Serial execution avoided two
  timing-sensitive launch-test timeouts seen with two workers. The final
  candidate suite passed without source changes after the cache correction.
- [x] (2026-10-24 11:56Z) DF12 findings in the new test support were resolved
  with a small case dataclass and explicit assertions. Skylos then identified
  `JournalSink.emit_trace` as unused; source tracing confirmed live protocol
  dispatch through `JournalTracer` and `JournalSpanExporter`. A narrow,
  evidence-backed entrypoint records this false positive in `pyproject.toml`.
  After separately provisioning the pinned Nose tool, the isolated lint gate
  passed, including the duplication check.
- [x] (2026-10-24 12:06Z) The exact EP-M1 documentation candidate passed
  isolated `make fmt`, `make check-fmt`, `make markdownlint` and serial
  `make nixie`. The current shared dictionary input was copied into the
  networkless sandbox and its SHA-256 verified; no formatting or generated
  `typos.toml` diff resulted.
- [x] (2026-10-24 12:17Z) The first fresh EP-M1 CodeRabbit review found one
  minor fixture-instruction ordering issue in the developers' guide. The text
  now directs maintainers to regenerate the fixture before inspecting its diff.
  Focused documentation gates and a fresh review of the correction are pending.
- [x] (2026-10-24 12:26Z) The correction passed all four isolated documentation
  gates and a fresh CodeRabbit review. Two suggestions to remove examples from
  private helper docstrings conflict with the repository's
  function-documentation requirement; the examples remain. No actionable review
  findings remain.
- [x] (2026-10-24 12:31Z) EP-M1 completed in commits `3455cee` and `e583fdc`.
  The first records the narrow Skylos protocol-dispatch entrypoint; the second
  commits the acceptance fixture checks, documentation and gate record.
- [x] (2026-10-24 12:52Z) EP-M2's contract tests and pure domain modules are
  drafted. The planned focused red command did not reach pytest because the
  networkless image lacked locked `filelock==4.2.4`; a derived image now
  carries the wheel verified against `uv.lock`. Run focused and milestone gates
  before marking EP-M2 complete.
- [x] (2026-10-24 14:40Z) O1 review found that the captured 0.14.0 windows
  fixture was not yet replayed through `attribute()`. Added a test that checks
  every fixture label and requires at least 200 controlled windows; it passed
  in the third full-suite run.
- [x] (2026-10-24 14:40Z) The first full-gate attempt used `/tmp/candidate`. Its
  networkless dependency setup could not resolve cached `hatchling` or `typos`;
  `make nixie` passed and markdownlint reported zero errors. Later runs use the
  established `/workspace` candidate path and a verified cache.
- [x] (2026-10-24 14:40Z) A second run from `/workspace` passed formatting,
  typechecking and Nixie. Lint found two `typing.cast` annotations, now fixed.
  The suite had 1,857 passes, 26 skips and one offline nested-`uv` cache
  failure; audit could not reach PyPI. Markdownlint passed, but spelling is
  inconclusive because the archive omitted ignored dictionary inputs and
  regenerated `typos.toml` in the disposable container.
- [x] (2026-10-24 14:40Z) The third sequential full run included the ignored
  spelling inputs. Formatting, typechecking, Markdown lint and spelling, and
  Nixie passed. Lint found one type-only import placement, now fixed. The full
  suite had 1,856 passes, 26 skips and two failures: the nested standalone `uv`
  command inherited a relative cache path, and a child-reap test hit its
  1-second timeout under automatic xdist concurrency. The Makefile sets
  `UV_CACHE_DIR=.uv-cache` inside `UV_ENV`, so a same-named make variable did
  not override the child environment. Retry with an absolute cache path in
  `UV_ENV` and one xdist worker. `make audit` could not resolve PyPI from the
  networkless candidate; run a dependency-only audit against the unchanged lock
  in a network-enabled container.
- [x] (2026-10-24 15:00Z) EP-M2's candidate passed formatting, format check,
  lint, typecheck, all tests (1,858 passed, 26 skipped), Markdown lint and
  spelling, and Nixie. The first full suite failures came from a relative
  nested-`uv` cache path and a 1-second child-reap timeout under automatic
  xdist; an absolute `UV_CACHE_DIR` in `UV_ENV` and one xdist worker resolved
  both. The final offline gates used rootless Podman, `Network=none`, no
  mounts, and a candidate archive at
  `c5c06e3d79ee975fe43c4263f8900f3aa890e9a51382a7b960bc141173fb2259`.
- [x] (2026-10-24 15:00Z) A dependency-only `pip-audit==2.10.1` run over the
  unchanged lock reported no known vulnerabilities. The rootless audit
  container used host networking because `slirp4netns` was unavailable; it had
  no mounts, no candidate source and only lock-derived requirements.
- [x] (2026-10-24 15:00Z) All three O1 mutation controls failed at their named
  assertions, with no collection or setup errors. Logs are
  `/tmp/mutation-buildlimits-cache-hit-prediction-1.out` through `-3.out`;
  `/tmp/mutation-buildlimits-cache-hit-prediction-index.md` records commands,
  mutant hashes and verdicts. The disposable source was restored between
  mutations and its final source hash matched its pre-mutation hash.
- [x] (2026-10-24 15:03Z) A post-hoc baseline reconstruction over commit
  `2c7c4abb630e782183788f7513663e850d87df05` plus the five M2 test/feature
  files reached pytest collection and reproduced the expected missing-module
  failure. It is recorded as reconstruction, not chronological TDD. Archive:
  `5799295b9f6aba8bd7141599ed890cdd86182017e7a4334b85c435fd034d7592`; log:
  `/tmp/o1-baseline-red-buildlimits-cache-hit-prediction.out`.
- [x] (2026-10-24 15:03Z) Manual review of the M2 modules, contracts,
  integration tests and documentation found no actionable defects.
- [x] (2026-10-24 16:00Z) CodeRabbit's review of M2 commit
  `327ae5ac2f74dbdee1d643db7cf80d7d9b4845a4` found five valid improvements:
  named CrossHair fields, named footprint lookups, public dataclass field
  discovery in the fixture helper, a shared BDD result guard and decoder-limit
  failures mapped to `parse_error`. Commit
  `080d9c05296018c0665db5428bfcfb877c52be16` applies those changes. The final
  candidate archive
  `3fa0205e1b45e36366edd7271e37ae6a575d8899b6464a889d4fdda4f7b897fa` passed
  formatting, lint, typecheck and all tests (1,861 passed, 26 skipped). The
  unchanged lockfile SHA
  `4274d6491e149c4668defe091e389d7eaa0f69cbd8fc2aebc7953d7f40554d13` remains
  covered by the dependency-only pip-audit 2.10.1 result: no known
  vulnerabilities. Gate logs use the
  `/tmp/*-buildlimits-cache-hit-prediction-final-diagnostic.out` prefix.
- [x] (2026-10-24 16:00Z) The local committed-diff CodeRabbit review of
  `080d9c05296018c0665db5428bfcfb877c52be16`, against
  `327ae5ac2f74dbdee1d643db7cf80d7d9b4845a4`, completed with zero findings
  across eight files. It ran as
  `coderabbit review --agent --fresh --committed`; PR #24 remained a draft, so
  no formal review was queued and no PR state or remote branch was changed.
- [x] (2026-10-24 17:11Z) EP-M3 red contracts were committed as `195ff77`.
  Formatting, Markdown/spelling, Mermaid, lint, typecheck and full one-worker
  tests passed in the isolated candidate. All 66 new EP-M3 test cases xfailed
  against the absent modules; the suite reported 1,861 passed, 26 skipped and
  66 xfailed. Dependency-audit evidence was reused because `uv.lock` was
  unchanged. Logs use the
  `/tmp/*-buildlimits-cache-hit-prediction-epm3-red-candidate-18.out` prefix.
- [x] (2026-10-24 18:28Z) EP-M3 implementation passed formatting,
  Markdown/spelling, Mermaid, lint, typecheck and the full one-worker test
  suite. The suite reported 1,942 passed, 26 skipped and 47 snapshots passed.
  The generated record snapshot was copied from the disposable candidate and
  its SHA-256 matched. Dependency-audit evidence was reused because `uv.lock`
  was unchanged. The final validated candidate archive is
  `/tmp/cache-hit-prediction-epm3-green-candidate-12-validated.tar`, SHA-256
  `b1e5a66a25c1c358f10cc1f7f6f673f46129523d882fc35255ad83681f5d1f28`; gate logs
  use the `/tmp/*-buildlimits-cache-hit-prediction-epm3-green-candidate-12.out`
  prefix.
- [x] (2026-10-24 18:56Z) EP-M4's sequencer and D13 red contracts are drafted.
  Candidates 1-4 passed formatting and documentation gates, then stopped at
  successive Ruff, Pylint and Ambrleaks findings. Candidate 4 identified
  high-entropy HMAC fields in the existing M3 snapshot. Its codec fixture now
  uses valid low-entropy digest values; the dedicated outcome-key tests
  continue to cover HMAC derivation. Candidate 5 reached Vulture, which treated
  the typed recorder accessor and Hypothesis collection alias as dead code;
  both references are now explicit. Typecheck and tests remain pending.
- [x] (2026-10-24 19:03Z) Candidate 6 passed Ambrleaks, Skylos and Vulture, then
  stopped at five duplication families in the new recorder tests. Their shared
  state setup and import boilerplate are being moved into a recorder-specific
  test helper before candidate 7; typecheck and tests remain pending.
- [x] (2026-10-24 19:04Z) Recorder fixtures, typestate readers and query setup
  now extend the existing `tests/outcome_test_helpers.py`; failure and
  state-machine cases use the shared module directly. Candidate 7's earlier
  archive stopped at Ruff on helper docstrings and assertions, plus one stale
  state-machine helper call. Returns contracts were documented, the command
  assertion moved to the test, and the machine now reuses its payload. Later
  candidates continued through the complete gate sequence.
- [x] (2026-10-24 20:52Z) Candidate 34 passed formatting, Markdown and
  spelling, Mermaid, lint, typecheck, and the full one-worker test suite. The
  suite reported 1,942 passed, 26 skipped, 18 expected strict xfails and 47
  snapshots passed. The xfails are the EP-M4 contracts against production
  modules that have not yet been implemented; the milestone remains incomplete.
  Typecheck exposed a test-machine field/method name collision, fixed by
  renaming the model field to `server_drain_count`. The dependency audit reused
  its passing result because `uv.lock` is unchanged (SHA-256
  `4274d6491e149c4668defe091e389d7eaa0f69cbd8fc2aebc7953d7f40554d13`).
  Candidate source SHA-256:
  `62a3dd7c399d2cb596eabc47510775202070d396481273afe75d94080147dd57`. Gate logs
  use the `/tmp/*-buildlimits-cache-hit-prediction-epm4-candidate-34.out`
  prefix; final exact-tree validation is pending after this progress entry is
  added.
- [x] (2026-10-24 21:09Z) Candidate 35 passed the sequential formatting,
  Markdown/spelling, Mermaid, lint, typecheck and full one-worker test gates.
  The test suite reported 1,942 passed, 26 skipped, 18 strict xfails and 47
  snapshots. Audit evidence was reused because `uv.lock` did not change. The
  formatter-only plan wrapping was applied before committing the red contracts
  as `eec3948`; the tested post-format candidate archive SHA-256 is
  `eb8e97eb809cad12ec146dab26875a6a7cce060f4ff038ff40643b413910acc2`. Logs use
  the `/tmp/*-buildlimits-cache-hit-prediction-epm4-candidate-35.out` prefix.
- [ ] (2026-10-24 21:19Z) EP-M4 implementation is in progress. The port,
  reducer, value, output and driver modules are drafted; 40 focused recorder
  and port contracts pass after the terminal-output helper moved into the
  output module to keep the reducer within the 400-line limit. `make fmt` and
  `git diff --check` pass. Full sequential gates remain pending.
- [ ] (2026-10-24 21:30Z) Candidate 36 formatting and format checks passed, but
  the full gate run exposed 69 lint findings and three test type errors. Its
  disposable environment also omitted tools from `PATH`, lacked one offline
  test dependency, and copied bytecode containing host paths. The log records
  1,973 passing tests, 12 skips, one dependency failure, and ten bytecode-based
  collection errors. Candidate 37 will fix code findings and use a clean,
  correctly seeded gate environment.
- [ ] (2026-10-24 21:36Z) Candidate 37 fixes the reported lint and type
  findings, splits terminal reading validation into a private helper, and
  preserves the accepted baseline after each valid attribution. All 40 focused
  recorder and port contracts pass; `git diff --check` is clean and every
  changed code file remains within the 400-line limit.
- [ ] (2026-10-24 21:43Z) Candidate 37's full sequential run passed formatting,
  format checks, Markdown/spelling, Mermaid, typecheck and the one-worker test
  suite (1,982 passed, 26 skipped, 47 snapshots). Audit evidence was reused
  because `uv.lock` matched its recorded SHA-256. Lint found five diagnostics:
  two unused imports, two unused unpacked names and one prohibited production
  `assert`. Candidate 38 removes those findings; gates remain pending.
- [ ] (2026-10-24 21:51Z) Candidate 38 passed formatting, format checks,
  Markdown/spelling, Mermaid, typecheck and the full one-worker suite (1,982
  passed, 26 skipped, 47 snapshots); audit evidence still matches `uv.lock`.
  Lint found one `raise-vanilla-args` diagnostic on the terminal-reading
  invariant. Candidate 39 uses the local message pattern, removes the EP-M8
  sample-failure port from EP-M4, and records `LaunchFailed` in the reducer
  event inventory. Full gates remain pending.
- [ ] (2026-10-24 22:00Z) Candidate 39 passed formatting, format checks,
  Markdown/spelling, Mermaid, typecheck and the one-worker test suite (1,980
  passed, 26 skipped, 47 snapshots). Audit evidence matched the lock hash. Lint
  found two Pylint C1803 findings for comparisons with empty lists. Candidate
  40 uses truthiness checks; full gates remain pending.
- [ ] (2026-10-24 22:07Z) Candidate 40 passed formatting, format checks,
  Markdown/spelling, Mermaid, typecheck and the full one-worker suite (1,980
  passed, 26 skipped, 47 snapshots); audit evidence matched `uv.lock`. Lint
  found four R9101 dispatch suggestions and one R9104 trivial-wrapper finding.
  Candidate 41 converts the dispatches to pattern matching and exposes the
  fake's close-list append callable directly. Full gates remain pending.
- [ ] (2026-10-24 22:17Z) Candidate 41 passed formatting, format checks,
  Markdown/spelling, Mermaid, typecheck and the one-worker test suite (1,980
  passed, 26 skipped, 47 snapshots); audit evidence matched the lock hash.
  Pylint passed at 10/10. Skylos could not see `OutcomeWindow.append` or
  `OutcomeRecorder` through the dynamic test imports. Candidate 42 imports both
  directly in their tests; full gates remain pending.
- [ ] (2026-10-24 22:26Z) Candidate 42 passed formatting, format checks,
  Markdown/spelling, Mermaid and the one-worker test suite (1,980 passed, 26
  skipped, 47 snapshots); audit evidence matched the lock hash. Typecheck found
  eight fake-port/event annotation errors, while Skylos still missed
  `OutcomeWindow.append` through the generic `Progress.window` field. Candidate
  43 annotates the fake callable, narrows dynamic gate events, and records the
  verified generic-field method entrypoint with its call path. Full gates
  remain pending.
- [ ] (2026-10-24 22:34Z) Candidate 43 passed formatting, format checks,
  Markdown/spelling, Mermaid and the full one-worker suite (1,980 passed, 26
  skipped, 47 snapshots); audit evidence matched the lock hash. Ruff found a
  banned `typing.Callable` and four unquoted cast types. Typecheck found that
  the port's `close` parameter was keyword-capable while the fake callable was
  positional-only. Candidate 44 uses `collections.abc.Callable`, quoted cast
  types and a positional-only port parameter. Pylint/Skylos remain unverified
  for candidate 43 because Ruff stopped the lint target first.
- [ ] (2026-10-24 22:43Z) Candidate 44 passed formatting, format checks,
  Markdown/spelling, Mermaid, typecheck and the full one-worker suite (1,980
  passed, 26 skipped, 47 snapshots); audit evidence matched the lock hash. Ruff
  found one type-only first-party import in the driver test. Candidate 45 moves
  `RecorderEvent` into `TYPE_CHECKING`; later lint stages remain unverified
  until the full lint target reaches them.
- [ ] (2026-10-24 22:52Z) Candidate 45 passed formatting, format checks,
  Markdown/spelling, Mermaid, typecheck, Pylint/DF12, Ambrleaks, Skylos and the
  full one-worker suite (1,980 passed, 26 skipped, 47 snapshots); audit
  evidence matched the lock hash. Vulture alone flagged the `TYPE_CHECKING`
  import referenced by a quoted `typing.cast` type. Candidate 46 adds a narrow,
  documented Vulture ignore entry; duplication and action validation remain to
  be reached by the full lint target.
- [ ] (2026-10-24 23:05Z) Candidate 46 passed formatting, format checks,
  Markdown/spelling, Mermaid, typecheck, Ruff/interrogate, both Pylint passes,
  Ambrleaks, Skylos, Vulture and the full one-worker suite (1,980 passed, 26
  skipped, 47 snapshots); audit evidence matched the lock hash. Duplication
  found two matching-import families and two repeated test setups; action
  validation was not reached. Candidate 47 documents the import overlaps and
  extracts the repeated setup code before another full gate run.
- [ ] (2026-10-24 23:15Z) Candidate 47 passed formatting, format checks,
  Markdown/spelling, Mermaid, typecheck and the full one-worker suite (1,980
  passed, 26 skipped, 47 snapshots); audit evidence matched the lock hash.
  Baseline Pylint found that the extracted retry helper had five parameters.
  Candidate 48 derives its controlled poll time from the query time, reducing
  the helper to four parameters; later lint stages remain unverified.
- [ ] (2026-10-24 23:27Z) Candidate 48 passed formatting, format checks,
  Markdown/spelling, Mermaid, every lint and action check, typecheck and the
  full one-worker suite (1,980 passed, 26 skipped, 47 snapshots); audit
  evidence matched the lock hash. Source manifests matched before and after the
  run. The fresh local CodeRabbit review is bound to this archive; its first
  request hit the service limit and is awaiting the permitted retry. Commit
  remains pending review.
- [ ] (2026-10-24 23:52Z) CodeRabbit completed the review of candidate 48's
  archive-derived snapshot (`2deaf17`) after its one rate-limit retry. It found
  six actionable issues: attached short-option parsing can falsify H3, only
  balanced windows may persist H3 summaries, mixed/ineligible readings should
  reset back-off, compaction must be linear, the cache-result vocabulary must
  have one source, and the tracing privacy assertion must inspect names and
  values. Candidate 49 adds the fixes and regression coverage before a fresh
  full gate and review pass. Review log:
  `/tmp/buildlimits-coderabbit-candidate-48.Lhga0F/coderabbit-buildlimits-cache-hit-prediction-candidate-48-retry-2.out`.
- [ ] (2026-10-25 00:04Z) Candidate 49 passed formatting, Markdown/spelling,
  Mermaid, typecheck and the full one-worker suite (1,982 passed, 26 skipped,
  47 snapshots). Ruff stopped `make lint` on the mock import form, so later
  lint stages did not run; audit evidence matched the lock hash. Candidate 50
  uses Ruff's requested `from unittest import mock` form before repeating the
  full sequential gates. Gate report:
  `/tmp/buildlimits-epm4-candidate-49-gate-logs/summary.md`.
- [ ] (2026-10-25 00:14Z) Candidate 50 passed formatting, Markdown/spelling,
  Mermaid, Ruff through Vulture, typecheck and the full one-worker suite (1,982
  passed, 26 skipped, 47 snapshots). The duplication gate found one repeated
  compactor-test setup, so action validation was not reached. Candidate 51
  combines those checks while preserving the byte cap and one-encoding
  assertions. Gate report:
  `/tmp/buildlimits-epm4-candidate-50-gate-logs/summary.md`.
- [ ] (2026-10-25 00:22Z) Candidate 51 passed formatting, Markdown/spelling,
  Mermaid, all lint and action checks, typecheck, and the full one-worker suite
  (1,981 passed, 26 skipped, 47 snapshots). Audit evidence matched the lock
  hash. Archive SHA-256 is
  `4d59135c617a345822e9dcf0e19f057c6408cf3d7b3eccf3274fd0f701a909a5`; the
  source manifest matched before and after gates. A fresh review remains before
  the EP-M4 commit. Gate report:
  `/tmp/buildlimits-epm4-candidate-51-gate-logs/summary.md`.
- [ ] (2026-10-25 00:35Z) Candidate 52's archive-bound review found eight
  actionable findings: two generation-contract gaps, header format/version
  classification, missing purity coverage, two sink-cleanup paths, and two
  documentation corrections. Candidate 53 addresses these and narrows the
  host-check subprocess suppressions. Review summary:
  `/tmp/buildlimits-coderabbit-candidate-52.kp7iuE/summary.md`.
- [ ] (2026-10-25 00:53Z) Candidate 53's full suite passed (1,987 passed,
  26 skipped, 47 snapshots), and typecheck passed, but lint found eight Ruff
  issues: one test-helper argument count and seven suppression comments naming
  rule codes. The candidate runner also inventoried 367 workspace files for a
  363-entry archive because `/workspace` contained image-side `.git`,
  `.claude/scheduled_tasks.lock` and typos dictionary files. Candidate 54 fixes
  the lint findings and will run from a clean candidate subdirectory. The
  candidate 53 archive SHA-256 is
  `6429574f5b8324d85b502d242ee16825464452571bffd612fcd6eca59641e0ce`.
- [ ] (2026-10-25 00:53Z) Candidate 54 ran in a clean source subdirectory and
  matched its archive manifest before and after the gates. Formatting,
  Markdown/spelling and Nixie passed; Ruff rejected two dynamic timestamp
  lookups in the recorder test helper. Typecheck and tests stopped at lint.
  Candidate 55 gives the gate event a structural timestamp type. Report:
  `/tmp/buildlimits-epm4-candidate-54-gate-logs/summary.md`; archive SHA-256:
  `a9ca927ab9348727ce0ef1ec44c744ac66b9a20b9ef007ec9ce1f53e26f29b0f`.
- [ ] (2026-10-25 00:56Z) Candidate 55 passed through Vulture, then the
  offline duplication gate failed to find its pinned Nose executable in the
  candidate-local tool cache and could not download it. Archive isolation
  matched exactly, but duplication, action validation, typecheck and tests did
  not run. Candidate 56 will seed the verified image Nose cache into the
  ignored candidate tool-cache path. Report:
  `/tmp/buildlimits-epm4-candidate-55-gate-logs/summary.md`; archive SHA-256:
  `df6366bd4fd79ccb4c124190987bad86b634b51035cc3a0392f35cc56ce128fb`.
- [ ] (2026-10-25 01:06Z) Candidate 56 passed all local gates in a clean
  candidate directory. The one-worker suite reported 1,987 passed, 26 skipped
  and 47 snapshots; the 363-entry source manifest matched before and after. The
  Nose 0.20.0 cache was mounted read-only and accepted offline by the
  duplication gate. A fresh archive-bound review remains. Report:
  `/tmp/buildlimits-epm4-candidate-56-gate-logs/summary.md`; archive SHA-256:
  `2a78e78c2e28a1b2ed38271a61bfc2709fe1d86347394c2da440dae4b03b820c`.
- [ ] (2026-10-25 01:21Z) Candidate 57's documentation gates passed with exact
  archive parity. Its archive-bound review found two minor discrepancies: a
  restart-machine assertion used a zero counter baseline, and this ADR
  described the trace schema as unchanged without naming the vocabulary
  extensions. Candidate 58 seeds the restart baseline from retained counters
  and clarifies that the fixed record fields remain stable while the bounded
  vocabularies extend. Review log:
  `/tmp/buildlimits-coderabbit-candidate-57.liwgAI/coderabbit-buildlimits-cache-hit-prediction-candidate-57.out`.
- [ ] (2026-10-25 01:44Z) Candidate 58 passed all local gates with exact
  archive parity: 1,987 tests passed, 26 skipped and 47 snapshots; audit
  evidence matched the lock hash. Its archive-bound review reported three
  trivial improvements: pattern-match decoder result unions, call `finish` on
  the span instance, and use `PurePosixPath` for Cargo path boundaries.
  Candidate 59 applies those changes and adds a root-path/boundary regression
  check. Report: `/tmp/buildlimits-epm4-candidate-58-gate-logs/summary.md`;
  review:
  `/tmp/buildlimits-coderabbit-candidate-58.2Y0TI3/coderabbit-buildlimits-cache-hit-prediction-candidate-58.out`.
- [ ] (2026-10-25 01:46Z) Candidate 59 passed formatting, docs/spelling and
  Nixie with exact archive parity, then Ruff found that `JournalTracer` was
  still imported at runtime despite being annotation-only. Typecheck and tests
  stopped at lint. Candidate 60 moves both tracing types under `TYPE_CHECKING`.
  Report: `/tmp/buildlimits-epm4-candidate-59-gate-logs/summary.md`; archive
  SHA-256: `7a2dfd85bb3ae23b2528f1f7368efec7f9ad8a9374fc3e718e7583bc49963984`.
- [ ] (2026-10-25 01:49Z) Candidate 60 passed formatting, documentation,
  Ruff, Pylint and Ambrleaks, then Skylos could not resolve the instance call to
  `_OtelSpan.finish` through `AdmissionAttempt.restart_span`. The existing
  adapter restart tracing tests cover this path. Candidate 61 declares that
  verified dataclass-field dispatch as a Skylos entry point. Report:
  `/tmp/buildlimits-epm4-candidate-60-gate-logs/summary.md`; archive SHA-256:
  `fb920ff5da0f2a9aa9d8f696526f329d518164d9b23d46b47b23beb1e5baab44`.
- [ ] (2026-10-25 01:52Z) Candidate 61 passed formatting, documentation,
  Nixie and lint through Vulture, then duplication found repeated setup across
  Cargo-home boundary assertions and repeated AST import scans in the purity
  guard. Candidate 62 shares one Cargo environment and inspects AST imports in
  a single traversal. Typecheck and tests stopped at duplication. Report:
  `/tmp/buildlimits-epm4-candidate-61-gate-logs/summary.md`; archive SHA-256:
  `0856a80a08f9b91bb942e8b4d5901070de86e43bb87aa832686cca3ea2c3cbfe`.
- [ ] (2026-10-25 02:29Z) Candidate 63 passed all local gates with exact
  archive parity: 1,990 passed, 26 skipped and 47 snapshots; audit evidence
  matched the lock hash. Its fresh review found that the driver changed the
  opaque ticket deadline, a straddled reading could seed a new generation's
  baseline, and sorting a trailing `--cfg` could raise `TypeError`. Regression
  tests reproduced all three; the fixes passed the focused 24-test suite.
  Candidate 64 full gates and review remain pending. Gate report:
  `/tmp/buildlimits-epm4-candidate-63-gate-logs/summary.md`; archive SHA-256:
  `2bf182381247b25f45ccfd6fb572b88a91210635eb394d1600d0f285f69dea1b`; review:
  `/tmp/buildlimits-coderabbit-candidate-63.yJl3Zl/coderabbit-buildlimits-cache-hit-prediction-candidate-63-retry.out`.
- [x] (2026-10-25 02:29Z) Candidate 64 passed every local gate with exact
  archive parity: 1,993 passed, 26 skipped and 47 snapshots; matching audit
  evidence reported no known vulnerabilities. The archive-bound review covered
  107 files and found no issues. Report:
  `/tmp/buildlimits-epm4-candidate-64-gate-logs/summary.md`; archive SHA-256:
  `1f1e19d4e898e6a8a5c98141d4fbb7c62551db7e42478591e39997161afb9a8b`; review:
  `/tmp/buildlimits-coderabbit-candidate-64.ktzLo9/coderabbit-buildlimits-cache-hit-prediction-candidate-64.out`.
- [ ] (2026-10-25 02:51Z) EP-M5 begins with the bounded child adapter. The
  existing `metrics_transport.run` is a blocking journal client and cannot
  serve the selector loop; the repository has no equivalent non-blocking child
  reaper. Red tests cover deadline cleanup, the output cap, the reaper bound
  and the `Popen` launch contract.
- [ ] (2026-10-25 02:52Z) The child adapter's red tests first failed at module
  collection, as expected. The implementation now passes all four focused
  contracts. Candidate 67 passed formatting, documentation and Nixie, then Ruff
  stopped lint on the missing `ValueError` documentation, path API, exception
  message and two property docstring forms. Candidate 68 applies those style
  fixes; it passed Ruff, then Pylint flagged the intentionally long-lived
  `Popen` handle. Candidate 69 documents that `ChildReaper` owns the process
  across loop ticks and keeps it outside a constructor-scoped context manager.
  Candidate 69 passed formatting, Markdown, and Nixie; Pylint required a
  failure message on one assertion, which candidate 70 added. Candidate 70
  passed those gates and the earlier lint stages, then Skylos identified the
  staged polling and cancellation methods before EP-M7 integration. Candidate
  71 records narrow entries linked to EP-M7 and adds explicit cancellation
  coverage. It passed those checks through Vulture, then the duplication gate
  found repeated long-running-child setup in two tests. Candidate 72 extracts
  one shared test helper for that setup. Candidate 72 passed every local gate;
  its archive-bound review exposed a real overflow in the existing repeated-
  failure back-off. A regression test reproduced `OverflowError` at 1,024
  failures. Candidate 73 caps the exponent before calculating the delay; the
  review's unrelated import-style suggestion in `tests/test_tracing.py` remains
  outside this change. Candidate 74 fixed the typed test construction and
  passed all local gates (1,999 passed, 26 skipped, 47 snapshots); its four
  trivial review suggestions are unrelated or scheduled for the EP-M7 docs set.
  The retry fix and child adapter are committed as `8fc69b3` and `d96518c`.
  Query-adapter tests now cover fixed argv/environment, fixture statistics,
  peer credentials, `/proc` failures and refused sockets; they first failed at
  collection because `outcome_queries.py` was not yet present. Candidate 75
  implements `SccacheStatistics` and `PeerServerProcess`, extends the fake
  client with the pinned statistics fixture, and bounds the proc environment
  read to 64 KiB. Its focused query, child and fake-client tests pass (17).
  Candidate 76 passed documentation gates, then Ruff reported excessive returns
  and incomplete proc-helper documentation. Candidate 77 extracts result
  mapping and documents the bounded socket and `/proc` failures. Its focused
  run found that `socket.setblocking` does not accept a keyword; the peer
  connect now uses zero-timeout non-blocking mode in candidate 78. Ruff then
  requested clearer malformed-stat detection and removal of an implicit
  `OSError` from the helper docstring; candidate 79 uses `rpartition` and the
  explicit exception contract. Pylint then flagged the proc fixture writer's
  six parameters; candidate 80 derives its PID and uses one missing-file
  selector. Pylint then rejected the forwarding `poll_children` wrapper;
  candidate 81 removes it because EP-M7's shell owns the injected reaper and
  will poll that shared instance directly. Candidate 81's duplication gate
  found repeated Unix-listener setup, query imports and equivalent generation
  tests; candidate 82 consolidates these through the shared listener helper,
  module-qualified types and one parameterized identity test. Candidate 82
  passed formatting, Markdown and Nixie, then Ruff flagged the helper's type
  imports; candidate 83 moves them into the type-checking block and uses
  `collections.abc.Iterator`. Candidate 83 passed all local gates (2,008
  passed, 26 skipped, 47 snapshots); matching audit evidence reported no
  vulnerabilities. Its archive-bound review found four actionable findings: the
  stale-socket test bypassed the shared listener helper, the helper table
  obscured their different ownership, the fake stats branch lacked a named
  operation, and the polling test stopped before its query deadline. Candidate
  84 addresses those findings, passes formatting and documentation gates, then
  lint flags the new fixture writer's missing return documentation. Candidate
  85 documents its zero return value and passes all local gates (2,008 passed,
  26 skipped, 97 warnings, 47 snapshots); matching audit evidence reported no
  known vulnerabilities. The fresh archive-bound review found no issues across
  seven paths. Archive SHA-256 is
  `580e94cc01f451c27d2b0832cfce62a87384c059a20a09ca06a06621f1fa015d` with 367
  files and manifest SHA-256
  `a78885f0aa3eb4050aa925ef6538b74f32875cffc3cebb740e6a2e305a30e5ec`. The query
  adapters and tests are committed as `56bc46b`.
- [x] (2026-10-25 06:30Z) The earlier stop-hook `FileNotFoundError` for
  temporary-directory discovery did not recur once usable temporary storage
  returned; candidate97 and candidate98 completed all temporary-file gates.
  Candidate96 stopped at Ruff because the runtime `Salt` pattern import was
  type-only; candidate97 corrected it and passed all deterministic gates. Its
  archive-bound review identified a corruption path when both a partial append
  and its `ftruncate` rollback fail. The new fault-injection test failed before
  the repair and reproduced corruption before shutdown. The writer now
  schedules compaction after append I/O errors. Candidate98 passed all gates,
  with 2,039 passed, 26 skipped and 47 snapshots; the matching-lock audit
  evidence reports no known vulnerabilities. Its archive-bound review found no
  issues. The writer is committed as `34ea33b`. Archive SHA-256 is
  `5f648f309cfb7898bc9131fa6d487cbafc2d959f8bfd336f1d45c24d0b84cd09`; its
  374-file manifest SHA-256 is
  `5718539323c8ce839586713d146a7cbbce78a4c68d953422898caf1cc6433b75`. Review
  log:
  `/tmp/buildlimits-coderabbit-candidate-98.fhUZ3T/coderabbit-buildlimits-cache-hit-prediction-candidate-98.out`.
- [x] EP-M0: ADR 004, fixed thresholds, roadmap phase 3, contents and stats
  fixture; review corrections from `9098857`, `684fffde` and `8e0bd40` are
  applied, and its documentation gates and review passed at that revision. The
  r6/r9/r13/r19 follow-up corrections and r21 formatter correction are recorded
  above; the r23 and r31 documentation gates and reviews passed.
- [x] EP-M1: real-binary feasibility and axiom check: **go**; implementation,
  code and documentation gates passed; milestone review passed; commits are
  `3455cee` and `e583fdc`.
- [x] EP-M2: pure statistics, footprints, attribution, and gate; implementation
  commit `327ae5a`, review follow-up `080d9c0`, final gates and local
  CodeRabbit review complete.
- [x] EP-M3: pure keys, records and the window ledger; implementation and
  full gates passed.
- [x] EP-M4: the reading sequencer over fake ports; implementation, full
  gates and archive-bound review passed in commit `636f3ed` (candidate 64).
- [x] EP-M5: adapters (non-blocking child, queries, generation, store
  writer). Implementation commit `34ea33b`; full gates and clean archive-bound
  review passed in candidate98.
- [ ] EP-M6: inert schema, configuration and tracing vocabulary
  (releasable).
- [ ] EP-M7: supervisor wiring, status, report, `h3` replay, systemd
  scenarios and documentation.
- [ ] EP-M8: H2 child sightings, the `h2` replay, and their documentation.
- [ ] G1 (owner): release, enable, collect, record H1, H2 and H3, and
  decide.
- [ ] EP-M9: W2 offline replay (H4 and H5), gated on G1.

## Surprises & discoveries

- S1. In sccache 0.14.0, every result counter for a request is updated under
  one `me.stats.lock().await` guard, inside the spawned task, before `Ok(res)`
  is returned (`start_compile_task`, around `server.rs` line 1376). The
  `CompileFinished` body is built only from that resolved future (around lines
  1022-1028). The cache-write counters are awaited later, but they are also in
  place before the reply. Further details:
  - `compile_requests` (in `call`, around line 864), the classification
    counter and the result counters are three separate lock acquisitions.
  - `Err(HttpClientError)` and a panic in the task count only
    `requests_executed`. This plan calls that the _silent_ footprint.
  - Without a server, `--show-stats --stats-format json` exits 0 and prints
    a full `ServerInfo` with zeroed statistics and the client's own
    `version`.
  - A failed query prints `sccache: error: ...` and exits 2.
  - `-vV` is classified `CannotCache("missing input")`. `--print`,
    `--version`, `-V`, `--explain`, `--help`, and an emit with neither
    `link` nor `metadata`, are classified `NotCompilation`.

  Evidence: quoted `server.rs`, `commands.rs` and `compiler/rust.rs` at
  `v0.14.0` (research pass of 2026-10-23). Impact: A2 and A3 hold from source,
  so D12 becomes a contingency only. D13 defines `empty_stats`, and the truth
  table gains the silent footprint.
- S2. RFC 0002 says `InvocationID` can be read "as one more property name on
  the `manager.show` call the supervisor already makes". That does not fit the
  code:
  - The only `show` callers are `ManagedSccache.accounted_drain` and
    `active_compiles` (`buildlimits/sccache.py` lines 221 and 253). They run
    only while a group is in `SERVER_DRAIN`, which is exactly when a quiet
    point cannot occur.
  - Reachability is an `AF_UNIX` connect (`sccache.py` lines 185-219), not a
    `show` call.
  - Adding the property to the drain call would turn the public-bus
    fallback's refusal (`bus_properties.service_properties`, line 177) into
    a drain fault.

  Impact: D2.
- S3. Every existing `systemctl` and `busctl` call is a blocking
  `subprocess.run` with a 3-second bound (`systemd._transport_bound`). The
  private manager connection on this host is known to fail at times, which is
  why the bus fallback exists. The panel measured a spawned `systemctl` at 10
  to 24 ms typically, 300 ms at p90 and 1.1 s at worst. Impact: D2 reads the
  generation without spawning a process.
- S4. `metrics_records._source_accepts_event` already accepts every
  supervisor event except `adapter_finished`.
  `tests/test_metrics_report.py::test_event_source_matrix` is parametrized over
  `sorted(EVENTS)`. Impact: `metrics_records.py` needs no change.
- S5. G0, the coverage bound, was measured read-only with the command in
  `Artefacts and notes`. Over seven days there were 473,503 `request_finished`
  records with `cache_path=cached`:
  - 30,795 (6.5%) finished with `queued == 0` and `server_drain == 0`;
  - 21,110 (4.5%) also had at most one group running or draining, which
    bounds the solo-window share;
  - the typical state at finish was 12 to 16 queued and 3 running.

  Impact: on this host, H1's coverage prediction (at least 20%) is very likely
  falsified, and the RFC's rule then makes H2 "the only route to coverage". The
  absolute count, however, is about 3,000 candidate solo windows a day. This
  raised open question Q1, which the owner answered with (b): add H2 child
  sightings to W1 (EP-M8).
- S6. `DrainLoop._failure` calls `fail_health` on every escaped exception.
  `MAX_INTERNAL_FAILURES` (20) only sets the operator recovery text. Impact:
  C-ISOLATE requires total entry points (D19).
- S7. Two one-second launch tests timed out in the isolated full suite with two
  workers. The same suite passed with one worker (1,763 passed, 26 skipped),
  with no source change between runs. Keep the candidate gate at one worker on
  this shared host.
- S8. The isolated `make audit` could not resolve its PyPI service. A separate
  metadata-only scan of the locked development dependency set found no known
  vulnerabilities (`filelock==4.2.4`). The Make target itself remains
  unavailable in the networkless candidate sandbox; the dependency metadata
  scan was performed separately, without installing or executing the candidate.
  The older `filelock==4.2.0` advisories are no longer present in the current
  lock.
- S9. Skylos cannot resolve `JournalSink.emit_trace` through the cross-module
  `_TraceSinkPort` protocol dispatch. `service` supplies the sink to
  `JournalTracer`, which supplies it to `JournalSpanExporter`, whose `export`
  calls the method. A precise `pyproject.toml` entrypoint records this live
  call path and the tests that exercise it; no runtime code changed.
- S10. `append_line` attempts to truncate a partial write back to the previous
  size, but the rollback can fail too. Without a pending-compaction marker, the
  next append can make the fragment a non-final JSON Lines row, so the next
  load rejects and moves aside the whole store. Candidate97's review exposed
  this path; a fault-injection test reproduced it before the repair. The writer
  now compacts its validated in-memory rows before accepting a later append.

## Decision log

Decisions D1 to D25 were proposals of this draft and took effect when the owner
approved the whole plan on 2026-10-23. Each deviation from the letter of RFC
0002 is marked, and EP-M0 repeats it in ADR 004. W1 (EP-M0 to EP-M8) is
approved to proceed. W2 (EP-M9) remains gated on the owner's G1 decision under
C-GATES.

- D1. Scope. Decision: W1 is EP-M0 to EP-M8, and W2 is EP-M9, gated on the
  G1 decision. Rationale: RFC acceptance approves W1 now and W2 at G1.
- D2. Deviation from RFC 0002 §1: the generation read. Decision: the _server
  generation_ is read synchronously, with no spawned process, on either side of
  each statistics query:
  1. a non-blocking `AF_UNIX` connect to the managed socket;
  2. `SO_PEERCRED` gives the listening server's PID;
  3. `/proc/<pid>/stat` field 22 gives the start time;
  4. `/proc/<pid>/environ` gives `INVOCATION_ID`.

  The generation is the triple `(INVOCATION_ID or "", pid, starttime)`. A
  refused connect means _no server_. Any other failure is
  `generation_unavailable`. Rationale (S2, S3):
  - It still reads the unit's own invocation identifier, which systemd
    places in every service process's environment.
  - It identifies the very process that answers the statistics query.
  - It catches a server restart within one invocation.
  - It costs a connect and two small `/proc` reads, which is the same kind of
    connect `is_reachable` makes every 5 s.

  Alternatives considered:
  - Spawning `systemctl show` twice per reading: rejected for the spawns,
    the latency tail and the fallback gap.
  - Reading the unit's cgroup `cgroup.procs`: rejected because the cgroup
    path is unknown while the unit is inactive.
- D3. Refinement. Decision: a reading during whose statistics query any
  cached group launched is discarded as `straddled_reading`. It is neither a
  window end nor a baseline. Rationale: admission keeps running during the
  query, so discarding keeps every launch in exactly one window.
- D4. Deviation in the vocabulary. Decision: `cache_result` gains three
  tokens for requests the server declined without compiling:
  - `declined` (`requests_not_cacheable`);
  - `not_compile` (`requests_not_compile`);
  - `unsupported` (`requests_unsupported_compiler`).

  Rationale: every Cargo build begins with a `-vV` probe, which the adapter
  submits as cacheable and the server declines (S1). These windows need a name
  for the count rule and for H3. `declined` avoids reading like an admission
  refusal, and `not_cacheable` stays a result of a compilation that ran.
- D5. Deviation in the vocabulary. Decision: `outcome_reason` gains
  `unsupported_version`. A document whose `version` is outside
  `SUPPORTED_SCCACHE_VERSIONS = frozenset({"0.14.0"})` is rejected, and
  recording disables itself until restart (D16). Rationale: the RFC requires
  the host check to be repeated after an upgrade.
- D6. Deviation in event shape; this replaces the draft's two-grain event.
  Decision: three supervisor events.
  - `cache_outcome` (group grain only): exactly one per cached launch while
    recording is on. Its fields are `cache_result`, `outcome_attribution`,
    `crate_origin`, `outcome_reason`, `runtime_seconds` and
    `window_groups`, `child_sighting` and `child_samples`.
  - `outcome_reading` (reading grain): emitted when the reading reason
    changes, and once a minute as an aggregate with the counters
    `readings`, `readings_skipped`, `readings_failed`,
    `child_samples_taken` and `child_sample_failures`.
  - `outcome_store`: carries `store_operation` (`load`, `append`, `compact`
    or `salt`), `store_reason`, `store_records` and `store_discarded`.

  The field names avoid the generic `origin` and `attribution`, because
  `FIELD_KINDS` is one flat namespace. Rationale: the panel found that a single
  event with two grains double-counts in reports and floods the journal.
- D7. Refinement for privacy. Decision: every digest is HMAC-SHA-256 under
  the salt. Rationale: the argument digest covers absolute paths.
- D8. Decision: a reading starts only when every gate clause holds (D20), and
  only when either no baseline exists or the open window holds at least one
  ended launch. Readings start at most once per `OUTCOME_READ_SECONDS = 1.0`,
  with exponential back-off to 60 s on repeated failure. The statistics query
  has a deadline of `OUTCOME_QUERY_SECONDS = 2.0`, and at most one query is in
  flight.
- D9. Deviation in the host-check method. Decision: run the real sccache
  0.14.0 binary on a private socket and a private `SCCACHE_DIR`, driven by a
  scripted fake `rustc` (`tests/fake_rustc.py`). The check is opt-in,
  time-boxed to 1.5 hours, and does not block other milestones, because the
  statistics fixture is captured read-only from the live managed socket in
  EP-M0. Fallback if the check is no-go: a documented operator procedure (a
  real cold-then-warm build through the live, admitted supervisor after
  release). That fallback can show trailing counters only indirectly, as
  `count_mismatch` spikes, so D12 then becomes mandatory. Rationale: compiling
  under a private server would bypass admission (C-HOST).
- D10. Deviation from RFC 0002 §2: the toolchain identity. Decision: the
  identity is pure and needs no input or output. It is the tuple of:
  - the HMAC of the compiler as given;
  - the HMAC of `RUSTUP_TOOLCHAIN`, or of `""` when it is unset;
  - the sccache Rust `CACHE_VERSION` (`"6"`);
  - the server `version` from the statistics document.

  The supervisor never runs `<compiler> -vV`, and never resolves or stats the
  compiler. Rationale:
  - Running a request-supplied compiler in the supervisor's control group is
    a new trust boundary.
  - Cargo already folds the compiler's `-vV` text into `-C metadata`, which
    the argument digest covers.
  - The panel found that the draft's filesystem fingerprint had no owner and
    no port.

  The residual confounder, a rustup channel name that has moved, is measured by
  H5, just as the RFC accepts for an in-place sysroot change.
- D11. Decision: the H1 to H5 thresholds in `Artefacts and notes` are fixed
  in ADR 004 in EP-M0, before any data is read. `outcome_replay` holds them as
  constants, and a test pins them to the ADR's text.
- D12. Contingency, conditional on A2. Decision: only if EP-M1 shows a
  trailing counter, a label is provisional until confirmed. A pure
  `confirm(provisional, next) -> Attribution` confirms it only when the next
  accepted window in the same generation balances exactly. The recorder holds
  at most one pending attribution. An unconfirmed label becomes `unattributed`
  with `count_mismatch`.
- D13. Decision on classification order. A reading's reason is the first of
  the following that applies:
  1. `timeout`: the query deadline expires.
  2. `read_error`: any non-timeout failure of the reading operation, including
     a query-launch failure, socket error, or transport read failure.
  3. `parse_error`: the output is not one JSON object with a `stats` object.
  4. `unsupported_version`.
  5. `generation_unavailable`.
  6. `generation_changed`.
  7. `empty_stats`: all counters are zero while the window holds a launch.
  8. `counter_decreased`.
  9. `ineligible_window`: the window holds an unclean group.
  10. `count_mismatch`: launch counts, classification counters and executed
      footprints do not balance: Δ`compile_requests` is not the number of
      launches; classification counters do not sum to it; or footprints
      cannot explain the requests.
  11. `mixed_window`.

  An all-zero baseline in a running generation is accepted, because a fresh
  server is all zero.
- D14. Deviation in the vocabulary. Decision: `outcome_reason` gains
  `mixed_window`, for a sound, balanced window that the counters cannot split.
  Rationale: the RFC requires `outcome_reason` to be `none` exactly when a
  label exists, and none of its nine tokens names this case.
- D15. Deviation in the vocabulary, from the panel. Decision:
  `outcome_reason` gains these tokens:
  - `straddled_reading` (D3);
  - `supervisor_stopped`, for a window still open at stop;
  - `no_baseline`, for launches that closed before the first baseline;
  - `generation_unavailable` (D2);
  - `window_overflow`, for more than 64 launches in one open window;
  - `recorder_disabled`, when the total wrapper caught an exception or
    `unsupported_version` fired.

  `no_quiet_point` is reserved for aggregate `readings_skipped`; it is not an
  `outcome_reason` token. Rationale: O2 (one event per launch) needs a reason
  for every path, and the panel found `no_quiet_point` overloaded three ways.
- D16. Decision on back-off. Repeated identical reading failures back off 1,
  2, 4 … 60 s, and the interval resets on success. `unsupported_version` and
  `recorder_disabled` stop readings until restart, and the status document and
  an `outcome_reading` event both say so.
- D17. Deviation from RFC 0002 §2: the store format and writer. Decision:
  the store is append-only JSON Lines at
  `$XDG_STATE_HOME/buildlimits/cache-outcomes.jsonl` (default
  `~/.local/state/buildlimits/`; directory mode 0700, files 0600).
  - The first line is a header: `{"format": "buildlimits-cache-outcomes",
    "version": 1, "salt_id": …, "written_at": …}`.
  - One writer thread owns every file operation. The loop enqueues appends
    into a bounded queue of 4,096 entries; on overflow the append is dropped
    with `save_failed`.
  - Each serialized record is at most 16 KiB. Compaction runs on the thread,
    at start, when appending would take the file above 12 MiB, and at stop. It
    drops records aged 30 days by `observed_at`, keeps the 10,000 newest by
    insertion sequence, then evicts the oldest remaining records until the
    file is at most 12 MiB. It then calls `fsync` and `os.replace` on a fixed
    temporary name.
  - If compaction fails, an append is allowed only when the file remains at or
    below 16 MiB; otherwise the append is dropped with `save_failed`. Records
    over 16 KiB are also dropped with `save_failed`.
  - On load, a torn final line is dropped, and a file over 16 MiB is
    `corrupt`. A `corrupt` or `version_mismatch` file is moved aside once to
    `cache-outcomes.jsonl.bad`. A file of a newer version is never
    overwritten.
  - The stop path enqueues compaction after `settle_token_request` and
    `requests.close_all`, then joins the thread for at most 1 s.

  Rationale: the panel showed that a once-a-minute rewrite with `fsync` writes
  up to 5.8 GB a day and can stall the loop for seconds. The RFC's "atomic
  save" is kept for compaction. In W1, layer 1 is the only writer, and the
  replay CLI gets a read-only loader.
- D18. Decision on rollback safety. EP-M6 is a releasable, inert plateau. It
  accepts `cache_outcomes` (`off` or `record`; `record` logs once that
  recording is not built in this release) and registers the events. Compatible
  with whom: the dev-env-rocky role's rendered `supervisor.toml` on a
  downgrade. Without this plateau, a downgrade with the key set exits 2 and
  stays down (R5). The users' guide documents removing the key before
  downgrading past it.
- D19. Decision on total entry points. Every `OutcomeShell` method that the
  loop calls is wrapped. On an exception the wrapper logs once, records
  `recorder_disabled`, switches to the null shell until restart, and returns.
  The hooks are placed after the admission call they observe:
  - the launch hook runs on the admitted branch, after `Groups.release`;
  - the emptied hook reads `(group_id, cached, clean)` into a frozen value
    before `group_emptied`, and calls the shell after `_apply`.
- D20. Decision on the gate. The pure function
  `outcome_gate.quiet_point(inputs) -> GateReason | None` requires every clause
  to hold:
  - `queued == 0`;
  - `cached_moving == 0`;
  - `server_drain == 0`;
  - `admission.cached`;
  - `cache_allowed()`;
  - recovery idle;
  - no restart barrier file;
  - no query already in flight.

  Recovery idle is read last, through `CacheRecovery.describe(now)["phase"]`,
  because `cache_recovery.py` has no headroom for a property. The `queued == 0`
  clause is the panel's: with a queue, the next launch follows within a
  millisecond, and the reading would straddle.
- D21. Deviation in the H3 evidence. Decision: `parser_class(argv)` predicts
  the first terminal result of sccache 0.14's Rust parser as `not_compile`,
  `declined` or `cacheable`. During the left-to-right argument scan, parse
  errors, `TooHardFlag`/`TooHardPath` (including `-`, `-o` and `--sysroot`),
  repeated `--emit`, unsupported crate types, `-C extra-filename` without a
  value, incremental compilation, an unsure target and multiple inputs are
  `declined`. `--explain`, `--help`, `--pretty`, `--print`, `--unpretty`,
  `--version` and `-V` are `not_compile` if the scan reaches them first. After
  scanning, missing input, `--out-dir`, `--emit` or `--crate-name` is
  `declined`, in that order. An emit set with neither `link` nor `metadata` is
  `not_compile`, including dep-info-only, asm-only and `--emit=` (one empty
  member). When no supported `rlib` or `staticlib` type is present, predict
  `declined`; unsupported emit kinds are `declined` only after the
  no-link/no-metadata check. Metadata-only is cacheable when the other required
  fields and a supported crate type are present. Cargo's `-vV` probe is
  `declined` as `missing input`.

  For every balanced window, mixed or not, the store also gets a _window
  summary_ record. It holds the launch count, Δ`requests_not_compile`,
  Δ`requests_not_cacheable`, and the predicted launch count for each class.
  Compare the two predictions with their counters separately, then use
  per-request rows from solo windows as a second check. Rationale: equal counts
  are necessary but not sufficient; the two refusal classes must not be
  collapsed into one counter.
- D22. Decision: `build-limits-cache-replay h3` ships in W1 (EP-M7), read
  only over the store. Rationale: G1 needs the H3 verdict, so it cannot wait
  for W2.
- D23. Superseded by the owner's Q1 answer (b) on 2026-10-23. The draft
  proposed that W1 collect no child sightings and report H2 as `not_collected`.
  W1 now collects them in EP-M8 (D24, D25), and `not_collected` is reported
  only for a store written before sampling existed, which cannot happen once
  EP-M8 ships.
- D24. Deviation from RFC 0002 §1 (ports). Decision: the RFC's
  unit-properties port becomes `ServerProcessPort`, with two synchronous
  operations on the same server process:
  - `generation()` is the D2 read;
  - `full_emit_metadata()` returns the `-C metadata` values of every
    full-emit compiler process now in the server's control group, or
    `SampleFailure`.

  The control-group directory comes from `/proc/<pid>/cgroup` of the
  generation's PID (`cgroups.parse_unified_path` and `cgroups.directory`),
  cached per generation. Its members come from `cgroups.member_pids`. Each new
  `(pid, starttime)` has its `/proc/<pid>/cmdline` read once and classified by
  the pure `outcome_sightings.full_emit_metadata`. A sample inspects at most 64
  processes, and the classification cache forgets a process once it leaves the
  group. Rationale: both operations read the one server process from `/proc`,
  so a sixth port would split a single concern, and the RFC's budget of five
  ports holds. The RFC already requires child arguments to be compared in
  memory and never stored, and this keeps that: only a closed-vocabulary
  sighting token and a sample count reach the record.
- D25. Partially superseded by D31. Its sampling cadence and H2 thresholds
  remain, but the child-to-entry matching and ambiguous-sighting rules are
  replaced by D31 after review.
  - Matching. `outcome_sightings.request_metadata(argv)` reads the request's
    own `-C metadata=X`, in any of the spellings `-C metadata=X`,
    `-Cmetadata=X`, `--codegen metadata=X` and `--codegen=metadata=X`, and
    keeps it in memory on the window entry. A process counts as a full-emit
    compile only if it is a `rustc` (or `clippy-driver`) whose `--emit` set
    includes `link` or `metadata`, or which has no `--emit` at all. A
    dep-info-only pass never counts.
  - Sampling. Every `CHILD_SAMPLE_SECONDS = 0.25`, while recording is on and
    `cached_moving > 0`, each running cached entry gains one sample. D31
    defines how shared metadata matches are classified. Failures back off
    from 0.25 s to 8 s and are counted.
  - Record and event. Each `cache_outcome` and each stored record carries
    `child_sighting` and `child_samples`; D31 adds `ambiguous` to the vocabulary.
  - The H2 classifier, applied by `build-limits-cache-replay h2` to stored
    records labelled `hit` or `miss`, walking each key in insertion order:
    1. Predict `miss` if `child_sighting` is `seen`.
    2. Otherwise predict `miss` if the key has at least one earlier `hit`
       and `runtime_seconds > max(1.0, 3 × median runtime of those earlier
       hits)`.
    3. Otherwise predict `hit`.

    Only records with `child_samples >= 1` whose key holds both outcomes in
    the store are evaluated, which is the RFC's "keys with both outcomes".
    An ambiguous child contributes no child clue; the runtime rule remains
    available. The child-only agreement excludes ambiguous and unavailable
    records, and the report gives their counts separately.
  - The verdict:
    - `supported` at 95% agreement or more;
    - `refuted` below 90%;
    - `inconclusive` in between;
    - `insufficient` below 2,000 attributed outcomes or 100 evaluable
      records.

    The report also gives the share of labelled misses that were `seen`,
    among `seen`/`not_seen` records, which tests the RFC's assumption that
    most misses leave a visible child, and the agreement of each clue on its
    own.

  Rationale: the RFC states H2's prediction and thresholds but not the
  classifier, and an unfixed classifier could be tuned to the data. Fixing the
  rule, the evaluable set and the minimum before reading keeps the verdict
  honest. The `max(1.0, …)` floor keeps a key with very fast hits from calling
  every dep-info-dominated hit a miss. What H2's verdict leads to is out of
  this plan's scope: if `supported`, a separate plan may emit `inferred`
  outcomes apart from labels (RFC I4); if `refuted`, a follow-up change removes
  sampling (G1 runbook).
- Considered and rejected: a "W1-lite" that defers the store, salt and key
  until G1, as the panel's alternative. It saves about 40% of W1's code if G1
  says stop, but delays H4 and H5 data by a whole collection period. G0 already
  answers the coverage risk it hedged, and more cheaply. The owner may still
  choose it at approval.
- Q1 (resolved 2026-10-23 by the owner, leynos): (b), add an H2
  child-sighting milestone to W1 now. G0 (S5) bounds solo coverage near 4.5%,
  below H1's 5% line, and the RFC's rule then makes H2 the only route to
  coverage. The other options were (a), proceed and decide H2 at G1, and (c),
  stop after EP-M0. The impact is EP-M8 (D24, D25), with W2 renumbered EP-M9.
- D26. Owner approval. Decision: the owner approved this whole plan,
  including D1 to D25 and all recorded deviations, on 2026-10-23. This
  authorizes W1 through EP-M8. EP-M9 remains gated on G1, and no later RFC
  workstream is authorized by this approval. Rationale: the explicit approval
  requested before implementation was supplied with the implementation request.
- D27. Review correction to D17. Decision: compact on the writer thread when
  appending would take the store above 12 MiB, retain at most 10,000 records
  within that byte budget, and cap serialized records at 16 KiB. If compaction
  fails, reject an append that would exceed the existing 16 MiB load limit.
  Rationale: start/stop-only compaction could make a valid long-running store
  look corrupt on the next start. This preserves D17's retention and durability
  policy while enforcing its existing file bound during a run.
- D28. Superseded by D30. The review correction established that metadata-only
  emit is accepted and that the no-link/no-metadata check precedes unsupported
  emit-kind validation.
- D29. Superseded by D30. The review correction separated `NotCompilation`
  from `CannotCache`, confirmed that dep-info-only, asm-only and `--emit=` are
  `NotCompilation`, and identified the two matching statistics counters.
- D30. Review correction to D21, D28 and D29. Decision: `parser_class(argv)`
  predicts the first terminal result of sccache 0.14's Rust parser as
  `not_compile`, `declined` or `cacheable`. During the left-to-right argument
  scan, parse errors, `TooHardFlag`/`TooHardPath` (including `-`, `-o` and
  `--sysroot`), repeated `--emit`, unsupported crate types, `-C extra-filename`
  without a value, incremental compilation, an unsure target and multiple
  inputs predict `declined`. `--explain`, `--help`, `--pretty`, `--print`,
  `--unpretty`, `--version` and `-V` predict `not_compile` if the scan reaches
  them first. After the scan, missing input, `--out-dir`, `--emit` or
  `--crate-name` predicts `declined`, in that order. An emit set with neither
  `link` nor `metadata` predicts `not_compile`, including dep-info-only,
  asm-only and `--emit=` (one empty member). When no supported `rlib` or
  `staticlib` type is present, predict `declined`; unsupported emit kinds
  predict `declined` only after the no-link/no-metadata check. Compare
  `not_compile` with `requests_not_compile` and `declined` with
  `requests_not_cacheable`; predicted `cacheable` requests must move neither
  refusal counter. Persist only the `parser_class` enum on request records and
  the predicted class counts in window summaries, never argv; do not add it to
  the journal event. The fixture's `not_cached` breakdown includes 363 `-`, 477
  `-o`, 54 `--sysroot` and 2,583 `crate-type` declines, within 6,271 total.
  Rationale: the earlier H3 set omitted parser branches already visible in the
  one read-only stats snapshot. A repository sweep found that
  `groups.compiler_argv` only removes wrapper layers; it does not parse rustc
  options. Keep the pure predictor in its own `outcome_parser.py`, scoped to
  sccache 0.14 Rust H3 evidence and reused by record creation and replay only.
  `ArgTarget::Unsure` may depend on a sibling `.json` file's existence, which
  an argv-only pure predictor cannot inspect; it conservatively declines a
  path-like target without that filesystem evidence and never retains its path.
- D31. Review correction to D25. Decision: build a multimap from metadata to
  active entries at each child sample. A full-emit child marks an entry `seen`
  only when exactly one active entry has its metadata. If more than one active
  entry shares the value, mark each `ambiguous`; that sample cannot label any
  of them `seen`. `ChildSighting` is `seen` if any unique match was observed,
  else `ambiguous` if any shared match was observed, else `not_seen` after a
  sample with neither kind of match, else `unavailable` if no metadata or no
  sample exists. The H2 classifier does not use an ambiguous sample as a child
  clue, but may still use runtime. Child-only agreement and the miss sighting
  share use only `seen`/`not_seen` records; the report gives ambiguous and
  unavailable counts. Add unit, property, replay and overlapping systemd
  coverage for identical active metadata. Rationale: a child argument can
  identify multiple active requests, and must not be assigned to each.
- D32. Review correction to RFC 0002 footnotes. Decision: number references
  by first appearance, with Rust `str::split`, sccache `parse_arguments`, the
  command response, Cargo environment variables, Cargo fingerprints, and the
  server counter updates in that order.
- D33. Review correction to cross-document contracts. Decision: ADR 004 D17
  owns one append-only JSON Lines store and its single writer; layer 2 reads
  those records and does not persist a second snapshot. RFC 0002 uses the same
  HMAC-SHA-256 digest and toolchain identity rules, and scopes child-sighting
  fields to per-launch events and records while `outcome_reading` carries only
  aggregate child-sampling counters. Rationale: the earlier RFC described a
  second writer, snapshot file and different identity inputs, so its readers
  could disagree with the W1 record writer and classifier.
- D34. EP-M1 test-support boundary. Decision: keep the real-server lifecycle in
  `tests/sccache_host.py` and move pure counter validation plus replay-window
  normalization into `tests/sccache_stats.py`. The latter is test-only and is
  not a runtime API. Rationale: the pinned check needs both process-lifecycle
  and counter-fixture support; keeping those responsibilities separate keeps
  each source file below 400 lines and the process harness focused.
- D35. Store append recovery. Decision: any append I/O error marks compaction
  pending; before accepting another row, the writer rebuilds the file from its
  validated in-memory records. If the repair fails, it rejects the next row.
  Rationale: a failed partial-write rollback must not let a later complete row
  turn the fragment into a corrupt non-final line. This preserves D17's
  single-writer and bounded-store design without changing the format or port.

## Outcomes & retrospective

The panel's verdict on the first draft (Pandalump, Wafflecat, Buzzy Bee,
Telefono, Doggylump and Dinolump, 2026-10-23) was ⚠️ proceed with conditions.
This revision meets those conditions with:

- a generation read that spawns nothing (D2);
- separate events with bounded volume (D6, D16);
- an append-only store on a writer thread (D17);
- total entry points and safe hook placement (D19);
- a pure gate with a `queued == 0` clause (D20);
- a corrected H3 predicate with window evidence (D21), and `h3` delivered
  before G1 (D22);
- a pure toolchain identity (D10), with `RUSTUP_TOOLCHAIN` salted
  (C-PRIV);
- no toolchain-tag dropping in W1, since H5 needs those records (Telefono);
- EP-M5 split into EP-M4 to EP-M7;
- the test scaffolding named explicitly;
- the self-contained tables in `Artefacts and notes`.

The pre-mortem scenarios, each with its mitigation now designed in:

- a once-a-minute `fsync` stall, answered by the writer thread;
- a rollback outage on an unknown key, answered by D18;
- six weeks of empty data from failing generation reads, answered by
  `generation_unavailable`, back-off, the status line and a 24-hour check in
  the G1 runbook.

EP-M5 implementation and evidence (2026-10-25):

- The child adapter, query adapters and outcome store writer are committed as
  `d96518c`, `56bc46b` and `34ea33b`. The developer's guide inventories the
  three store modules; no dependency was added.
- Candidate98 passed formatting, format checks, Markdown and spelling, Nixie,
  full lint including duplication and action validation, typecheck, and the
  one-worker test suite (2,039 passed, 26 skipped, 97 warnings and 47
  snapshots). Matching-lock audit evidence reports no known vulnerabilities.
- The append rollback regression failed before the fix because the second row
  made the store corrupt before shutdown, and passed after compaction was
  scheduled. The archive-bound review covered nine paths and found no issues.
  Archive and manifest hashes are recorded in `Progress`.

EP-M0 conformance and evidence (2026-10-24):

- The initial ADR 004 commit, `ddfaeae`, recorded D2 to D25 and fixed Table 1
  before the live query; its status remains Proposed until EP-M8 merges. Review
  corrections D27 to D32 are recorded in this ExecPlan.
- The sccache 0.14.0 fixture came from one read-only query to the managed
  socket. `cache_location` and `basedirs` were redacted before writing the
  fixture. G0's 4.5% solo-window bound remains the prior journal measurement in
  S5; it was not re-measured.
- The first final gate run passed `make check-fmt`, found `unlabeled` in ADR
  004 (corrected to `unlabelled`), and timed out on three existing diagrams in
  `make nixie`. The container reports 24 CPUs and Nixie's default concurrency
  is CPU count minus one. The final run passed `make check-fmt`,
  `make markdownlint` including spelling, and `make nixie` with its documented
  maximum concurrency set to one. Logs are
  `/tmp/buildlimits-epm0-final.LiUuHY/check-fmt.out`,
  `/tmp/buildlimits-epm0-final.LiUuHY/markdownlint.out` and
  `/tmp/buildlimits-epm0-final.LiUuHY/nixie.out`.
- Spelling used the cached `typos-config-builder` 0.1.3 executable because
  uv's Git resolver cannot contact GitHub in the networkless container.
  `uv sync` regenerated unrelated `uv.lock` drift during formatting and gates;
  it was restored each time. No code mutation results apply to this
  documentation and fixture work.

EP-M2 conformance and evidence (2026-10-24):

- The post-review implementation changes are isolated in `080d9c0`. The final
  candidate passed the deterministic code gates and the full one-worker suite;
  the O1 mutation controls ran before the review follow-up and failed at their
  intended assertions as recorded above. No dependency changed, so the pinned
  dependency-only audit result remains applicable.
- The local CodeRabbit pass inspected the committed range from `327ae5a` to
  `080d9c0` and found no further issues. The draft PR's remote head was not
  changed. Follow-up plan reviews found a serial-comma correction and an
  independent-clause comma, fixed in `6f3c40e` and `c72073c`. The docs gates
  passed for both; the final local review of `c72073c` found no further issues.
  The draft PR's remote head remains unchanged.

## Context and orientation

buildlimits is a Python 3.14 package (`buildlimits/`) that rations Rust
compiles on a shared host. The terms below are used throughout this plan.

- Cargo runs `build-limits-rustc` (the _adapter_, `buildlimits/adapter.py`)
  as `RUSTC_WRAPPER`. The adapter sends a _compile request_ (`argv`, `cwd`,
  `env`, `label`, `cacheable`) to the _supervisor_ over a Unix socket. The
  request's `env` is the caller's complete environment.
- The supervisor (`buildlimits/supervisor.py`, `Supervisor`) is an
  imperative shell around a pure _admission core_ (`buildlimits/admission.py`,
  `Admission`). Admission returns _effects_ (`buildlimits/effects.py`), which
  the shell carries out through `buildlimits/effect_dispatch.py`.
- A _token_ is a byte from the shared pool device `/dev/guild`. Each
  admitted request becomes a _group_ (`admission_state.Group`, `gN`), run in a
  systemd transient _scope_. The token returns when the scope is empty.
- A group is _cached_ when admission composed it with the _managed sccache
  server_ (`buildlimits-sccache.service`, socket
  `$XDG_RUNTIME_DIR/buildlimits/sccache.sock`). `Groups.launch_spec`
  (`buildlimits/groups.py` line 283) prefixes `settings.sccache_executable` and
  sets `SCCACHE_SERVER_UDS`. The client writes to the caller directly, so the
  supervisor never sees a hit.
- `Counts` (`admission_state.py` line 25) is an identifier-free snapshot.
  `queued` counts waiting requests, `cached_moving` counts cached groups
  running or draining, and `server_drain` counts groups retained in
  `SERVER_DRAIN`. A cached group that ended abnormally keeps its token until
  `ManagedSccache.accounted_drain` proves the server stopped and empty.
  `Group.may_orphan_server_work` (line 88) is true when a group was cancelled,
  has no recorded exit, or was signalled. Its negation is the RFC's _clean_
  ending.
- The loop:
  - `Supervisor.serve` runs `DrainLoop.run` with `TICK_SECONDS = 0.1`. Each
    cycle selects, dispatches, and calls `Supervisor._tick` (line 326).
  - `_tick` ends with `supervisor_polling.poll_services`, which runs
    `_poll_server` (every 1 s), `_poll_pool` (2 s) and `_poll_cache` (5 s).
  - At lines 341-343, `_tick` calls `groups.remove(group_id)` and then
    `_apply(admission.group_emptied(group_id))` for each emptied scope.
  - `DrainLoop._failure` calls `fail_health` on any exception that escapes a
    cycle (S6).
  - `SIGCHLD` wakes the selector (`signal.set_wakeup_fd`). Group reaping is
    per process ID, so a new child of the supervisor is never reaped by
    group code.
  - `pool.py` already runs a daemon reader thread, the precedent for a
    worker thread.
- `ServerWatch` (`buildlimits/server_watch.py`) owns reachability
  (`admission.cached`, refreshed every 5 s by connecting to the socket), the
  recovery hold (`cache_allowed()` is false during it) and drain release.
  `CacheRecovery.describe(now)["phase"] == "idle"` is the only recovery-idle
  predicate. The restart barrier is the file `settings.recovery_marker`.
- Stop: `Supervisor.serve` returns from the loop, then calls
  `supervisor_shutdown.settle_token_request` and `requests.close_all`
  (`supervisor.py` lines 219-220).
- Metrics:
  - `metrics_schema.py` holds closed tables (`EVENTS`, `VOCABULARIES`,
    `DURATIONS`, `COUNTERS`, `BOOLEANS`), and `FIELD_KINDS` is derived from
    them.
  - `metrics.Metrics.emit` validates each field and drops what fails.
  - `metrics_report.report` builds the sections. `_cache` is the model, and
    `metrics_pressure_report` shows an optional section.
  - `metrics_cli._render` prints every top-level section.
  - `journalctl --lines` caps a query at `--max-records`, 10,000 by default.
    This host writes about 69,000 cached finishes a day, so the default
    covers only a few hours.
- Tracing: `tracing.JournalTracer.start(operation)` refuses an operation
  that is not in `tracing_export.OPERATIONS`. `_bounded_attributes` keeps
  `outcome`, `failure_category`, `stop_mode` and `retry_attempt` from the
  `tracing_export` vocabularies (ADR 003). Spans are timed live, between
  `start` and `finish`.
- Settings: `config.SupervisorConfig` is a frozen dataclass. An unknown key
  or a bad value stops start-up with exit 2. Enumerations are `str` fields
  checked by `_RANGE_RULES`.
- Clock: `timing.Clock` is a frozen dataclass of callables (`now`, `sleep`).
- Tests: everything lives in `tests/`.
  - Hypothesis state machines: `tests/test_admission.py::AdmissionMachine`.
  - CrossHair contracts: `tests/crosshair_contracts.py`, run by
    `tests/test_crosshair.py`.
  - Syrupy snapshots: `tests/__snapshots__/`.
  - pytest-bdd features: `tests/features/`.
  - Opt-in host acceptance is marked `systemd_user`.
    `make test-systemd-user` sets `BUILD_LIMITS_HOST_ACCEPTANCE=1`, and the
    skip helper is `tests/systemd_harness.py::systemd_user_marks`.
  - The global pytest timeout is 30 s.
  - `tests/fixtures/fake_sccache.py` has only `server` and `client` modes.
  - `tests/toolchain_trees.py::make_toolchain` creates empty toolchain
    files.
  - `make lint` runs strict Skylos and Vulture dead-code checks and a
    duplication gate, whose allowances live in `pyproject.toml`.

## Conformance basis

- Terms of Reference: none exists for this repository. RFC 0002 supplies the
  upstream intent.
- Technical design: RFC 0002 (`docs/rfcs/0002-cache-hit-prediction.md`),
  accepted 2026-10-23 at `42f40e4`. There is no separate `*-design.md`; the
  developer's guide is the living interface reference.
- ADRs:
  - ADR 003 (bounded tracing) governs the spans. ADR 004 amends its scope
    for the new operations.
  - dev-env-rocky ADR 011 governs token coverage.
  - This plan creates ADR 004 (`docs/adr-004-cache-outcome-recording.md`). Its
    initial commit `ddfaeae` recorded D2 to D25 and fixed the thresholds before
    the live stats fixture was captured. Review corrections D27 to D32 are
    recorded in this ExecPlan.
- Standards: `AGENTS.md`, `docs/developers-guide.md`,
  `docs/documentation-style-guide.md` and `docs/scripting-standards.md`.

The traced identifiers are:

- RFC-GOAL-1: record outcomes soundly. RFC-GOAL-3: decide with evidence.
- RFC-I1 to RFC-I4: the layer 1 invariants.
- RFC-PORTS: the five ports (clock, statistics, server process, store and
  salt). D2 and D24 turn the unit-properties port into the server-process port.
- RFC-OBS: the events, reasons, spans and report.
- RFC-STORE: the store's path, bounds, ageing, atomicity and single writer
  (refined by D17).
- RFC-PRIV: the salt, the file modes and the ban on export.
- RFC-KEY: §2's key as used by layer 1 records (refined by D10).
- RFC-H1 to RFC-H5, RFC-W1 and RFC-W2.
- RFC-COMPAT: the off-by-default key and the schema-first rule.

```plaintext
RFC-GOAL-1 -> RFC-I4 -> EP-M2 outcome_attribution.attribute
  -> tests/test_outcome_attribution.py::test_truth_table, ::test_every_mixed_multiset_is_unattributed
RFC-GOAL-1 -> EP-M4 outcome_recorder -> tests/test_outcome_recorder_machine.py::RecorderMachine (O2, O3)
RFC-I1 -> C-OBS -> EP-M7 -> tests/test_outcome_boundaries.py::test_decision_modules_never_reach_outcomes,
  tests/test_outcome_noninterference.py::test_admission_trace_identical
RFC-I2 -> C-NOSTART -> EP-M2/EP-M5 -> tests/test_outcome_gate.py, tests/test_outcome_queries.py::test_argv_is_show_stats_only
RFC-I3 -> C-ISOLATE, C-TOKEN -> EP-M7 -> tests/test_outcome_loop.py::test_loop_admits_while_query_stalls,
  tests/test_outcome_totality.py::test_raising_shell_never_strands_a_token
RFC-PORTS -> EP-M4/EP-M5 -> tests/test_outcome_ports.py::test_every_port_failure_has_its_reason
RFC-STORE, RFC-PRIV -> EP-M3/EP-M5 -> tests/test_outcome_records.py, tests/test_outcome_store.py,
  tests/test_outcome_store_salt.py, tests/test_outcome_store_writes.py,
  tests/test_outcome_key_properties.py
RFC-KEY -> EP-M3 -> tests/test_outcome_key.py
RFC-OBS -> EP-M6/EP-M7 -> tests/test_outcome_metrics.py, tests/__snapshots__/test_metrics_report.ambr,
  tests/__snapshots__/test_status_render.ambr
RFC-W1 host check -> EP-M1 -> tests/test_sccache_real_binary.py (opt-in)
RFC-H1, RFC-H3 -> EP-M7 -> build-limits-metrics cache_outcomes, build-limits-cache-replay h3 -> G1
RFC-H2 -> Q1 (b), D24, D25 -> EP-M8 -> tests/test_outcome_sightings.py, tests/test_outcome_sightings_properties.py,
  tests/test_outcome_replay_h2.py, build-limits-cache-replay h2 -> G1
RFC-W2, RFC-H4, RFC-H5 -> EP-M9 -> tests/test_outcome_replay.py, tests/__snapshots__/test_outcome_replay.ambr
RFC-COMPAT -> C-DEFAULT, C-SCHEMA, D18 -> EP-M6 -> tests/test_outcome_config.py,
  tests/test_metrics_records_compatibility.py (extended)
```

## Verification plan

### Current evidence

- EP-M0's ADR 004 threshold decision is committed before the fixture's single
  read-only query. The fixture pins the complete sccache 0.14.0 JSON shape and
  redacts host-specific `cache_location` and `basedirs` values.
- G0 is the existing read-only journal measurement recorded in S5; no new
  journal query was needed for EP-M0.
- EP-M5 candidate98 passed the full sequential local gates and archive-bound
  CodeRabbit review. Its 374-file candidate had exact before/after parity;
  2,039 tests passed, 26 were skipped and 47 snapshots passed. Audit evidence
  matches `uv.lock` and reports no known vulnerabilities. The archive and
  manifest hashes and review log are recorded in `Progress`.
- EP-M0's `make check-fmt`, `make markdownlint` including spelling, and
  `make nixie` passed sequentially. Nixie used the documented one-worker limit
  to avoid timeouts from the container's 24-CPU default. Exact logs are listed
  under `Outcomes & retrospective`.

### Axioms

- A1. sccache 0.14.0 moves its counters as in Table A (`Artefacts and notes`).
  Source: RFC 0002 §1 and S1. EP-M1 confirms hit, miss and declined on the real
  binary.
- A2. Every result counter for a request is updated before the reply, so a
  reading taken after the client exits sees the whole footprint. Source: S1.
  EP-M1 confirms it over 200 exits. If it fails, D12 activates.
- A3. Each `Compile` request moves `compile_requests` once, then exactly one
  of `requests_unsupported_compiler`, `requests_not_compile`,
  `requests_not_cacheable` or `requests_executed`. An executed request moves at
  most one result footprint, and none for the silent paths. A panic in
  `compiler_info` moves `compile_requests` alone, which the count rule rejects.
  Source: S1.
- A4. `SO_PEERCRED` on a connected `AF_UNIX` stream socket returns the
  credentials the listening process had when it called `listen(2)` (`unix(7)`).
  systemd sets `INVOCATION_ID` in every process of a service invocation
  (`systemd.exec(5)`). `/proc/<pid>/stat` field 22 is the start time in clock
  ticks since boot (`proc(5)`). The EP-M7 systemd scenario and EP-M1 exercise
  these facts.
- A5. `sccache --show-stats` uses `connect_to_server` and never starts a
  server (S1, RFC C5).
- A6. A scope reported empty means the sccache client has exited (the
  existing `group_drain` contract).
- A7. Cargo sets `CARGO_PKG_NAME`, `CARGO_PKG_VERSION` and
  `CARGO_MANIFEST_DIR` for every compiler invocation, and folds `rustc -vV` into
  `-C metadata` (Cargo Book).
- A8. `os.replace` within one directory is atomic on Linux local
  filesystems. A single `write` of one line under `O_APPEND` either lands whole
  or leaves a torn tail, which the loader drops.
- A9. For a miss, sccache 0.14.0 runs the full-emit compile as a descendant of
  the server, inside the unit's control group. That process's argv keeps the
  request's `-C metadata` value and an `--emit` that includes `link` or
  `metadata`. The hash-key pass of every request, hit or miss, runs with
  `--emit=dep-info` only. Source: RFC 0002 C3 and its discussion of Table 1.
  EP-M1 confirms this with a fake `rustc` that sleeps on full emit.
- A10. Reading `/proc/<pid>/cmdline` or `/proc/<pid>/stat` for a process that
  has just exited fails with `ENOENT` or `ESRCH`, or returns empty text, and
  never returns another process's data. A `(pid, starttime)` pair guards
  against PID reuse (`proc(5)`).

### Obligations

Each obligation names its method, artefact, evidence and non-vacuity check.

O1. Footprint decoding is sound. A solo window's label is the true outcome of
its request. A unanimous window's label is true for every request in it. Every
window whose delta is a sum of two or more distinct footprints is
`unattributed`.

- Method: exhaustive enumeration, plus a CrossHair contract.
- Domain: every multiset of size 1 to 4 drawn from Table A's thirteen
  footprints.
- Artefacts:
  - `tests/test_outcome_attribution.py::test_truth_table`;
  - `tests/test_outcome_attribution.py::test_every_mixed_multiset_is_unattributed`;
  - `tests/test_outcome_attribution.py::test_captured_windows_replay_to_their_known_outcomes`;
  - `tests/crosshair_contracts.py` contracts listed below.
- Red-run evidence: the original command did not reach pytest during initial
  sandbox setup because the networkless image lacked a locked dependency. A
  post-hoc reconstruction against baseline commit
  `2c7c4abb630e782183788f7513663e850d87df05`, with the five M2 test/feature
  files overlaid, reached collection and failed with the expected missing
  `buildlimits.outcome_attribution` module. This is not chronological TDD. The
  first collected focused run passed after implementation; a later CrossHair
  and boundary run exposed two test defects, which were fixed.
- Non-vacuity:
  - The enumeration must produce at least one solo window of each class.
  - It must produce one unanimous window of each unanimous class (hit, miss,
    not_cached, not_cacheable, declined).
  - It must produce at least 500 mixed multisets.
  - The RFC's ambiguous pair is a named row, and a solo silent window must
    be `count_mismatch`.
- CrossHair proof inventory in `tests/crosshair_contracts.py`:
  - `unanimous_hit_windows_keep_the_hit_label` checks 2 to 4 identical hit
    footprints through production `attribute()`.
  - `cache_errors_are_not_dropped_from_a_solo_footprint` checks one
    cache-error footprint retains the `error` label.
  - `repeated_cache_errors_are_not_unanimous` checks 2 to 4 identical cache
    errors remain unattributed.
  - `decoding_never_labels_mixed` checks every four-launch hit/miss mixture
    with 1 to 3 hits remains unattributed.
  - `tests/test_crosshair.py` runs
    `python -m crosshair check --analysis_kind=asserts --per_condition_timeout=20`
    against `tests/crosshair_contracts.py`, with a 100-second subprocess
    timeout. The lock pins `crosshair-tool` 0.0.111; the gate used Python
    3.14.8. These bounded symbolic contracts are not unbounded proofs; the
    separate exhaustive test enumerates multisets of up to four Table A rows.
- Mutations and their failing tests:
  - Relabel the compilation-only footprint as `hit`;
    `test_truth_table[not_cached]` must fail.
  - Drop the miss footprint with `cache_misses` and `cache_errors`;
    `test_truth_table[miss_read_error]` must fail.
  - Allow a unanimous `error`;
    `test_unanimous_error_window_remains_unattributed` must fail.

  The three logs are `/tmp/mutation-buildlimits-cache-hit-prediction-1.out`
  through `-3.out`; the index is
  `/tmp/mutation-buildlimits-cache-hit-prediction-index.md`.

O2. Exactly one event per launch. Every cached launch made while recording is
on yields exactly one `cache_outcome`. It comes from its window's close, or from
`supervisor_stopped`, `no_baseline`, `window_overflow` or `recorder_disabled`.

- Method: a Hypothesis `RuleBasedStateMachine` over the recorder (EP-M4)
  with fake ports.
- Rules:
  - a launch (cached or direct), a launch failure, a clean exit, a signalled
    exit and a cancel;
  - `SERVER_DRAIN` and its release;
  - a reading in each port outcome (prompt, slow, abandoned, failing);
  - a straddling launch, a server restart, and more than 64 launches;
  - an injected exception, and stop.
- Artefact: `tests/test_outcome_recorder_machine.py::RecorderMachine`.
- Non-vacuity: `hypothesis.event` labels must include each of these:
  - a solo, a unanimous, a mixed and a rejected window;
  - an abandonment, a straddle, a restart and an overflow.

  A teardown check over the `RecorderMachine.TestCase` run fails if any label
  is missing.
- Mutation: treating a straddled reading as a baseline must produce a
  double count that the machine finds.

O3. Labels are sound against a model server. Every emitted label equals the
model's truth. This holds under overlapping groups, abnormal endings, restarts
(one of which places the new generation's counters above the old), and the D12
path when it is active.

- Method: the same machine, whose model server keeps per-request truth.
- Non-vacuity: at least one label per run.
- Mutation: skip the generation check. A restart above the old values must
  then yield a wrong label.

O4. Non-interference (I1). The admission input trace (the sequence of
`Admission` method calls and arguments), the dispatched effects and the
token-return ticks are identical with recording on and off.

- Methods:
  - a differential state machine driving two full shells with a fake pool,
    fake groups and fake ports;
  - a transitive import-closure test over both `import` and `from`
    statements;
  - the reverse rule: outcome domain modules never import `admission`.
- Artefacts: `tests/test_outcome_noninterference.py` and
  `tests/test_outcome_boundaries.py`.
- Non-vacuity: the run must reach a cached launch, a `SERVER_DRAIN` and a
  completed reading.
- Mutation: the shell delays `token_arrived` while a query is in flight. The
  differential must fail.

O5. Loop liveness (I3). While a statistics query stalls for its whole deadline,
the loop still accepts a request, admits it and returns a token. The child is
killed and reaped, and its pipe closed.

- Artefact: `tests/test_outcome_loop.py::test_loop_admits_while_query_stalls`.
- Evidence:
  - at least 10 loop iterations during the stall;
  - one admission and one token return;
  - `waitpid` reports the child reaped;
  - no descriptor left open (`descriptor_guard`).
- Non-vacuity: substituting a blocking `subprocess.run` makes the iteration
  count 0.

O6. Vocabulary completeness. A test provokes and asserts each token of
`outcome_reason`, `store_reason`, `evaluation_reason`, `cache_result`,
`store_operation` and the gate reasons, and each Table 2 port failure. The
exception is `inferred`, which is asserted never to be emitted.

- Artefacts: `tests/test_outcome_reasons.py` and
  `tests/test_outcome_ports.py`.
- Non-vacuity:
  - A meta-test compares the parametrized IDs with both
    `metrics_schema.VOCABULARIES` and the `typ.Literal` aliases (through
    `typ.get_args`).
  - A timed-out reading asserts that no label exists, never that a label is
    unchanged.

O7. Privacy. No stored or journalled value contains a path separator or any
component of a generated local path. The paths cover the manifest directory,
`CARGO_HOME`, the compiler as given, `RUSTUP_TOOLCHAIN` as a path, and path
arguments. Without a salt no record is written, and the `cache_outcome` event
is unchanged.

- Method: Hypothesis over those generators.
- Artefact: `tests/test_outcome_key_properties.py`.
- Non-vacuity: `hypothesis.event` must classify registry, git and path
  origins, and a path-valued `RUSTUP_TOOLCHAIN`.
- Mutation: storing the raw input to the `path:` digest must make the
  property fail.

O8. Store bounds and durability:

- After compaction, the file holds at most 10,000 records, none of them with
  `observed_at` older than 30 days.
- Each record line is at most 16 KiB. When appending would take the file above
  12 MiB, the writer compacts it to at most 12 MiB. A successful append never
  takes the file above 16 MiB, including after a compaction failure.
- Eviction follows insertion sequence.
- A torn tail line is dropped.
- A version mismatch moves the file aside, and a newer file is never
  overwritten.
- A wall-clock jump of more than one day in either direction skips ageing,
  with `clock_skew`.
- A changed salt reports `salt_changed`.
- A record tagged with another toolchain identity is excluded from history and
  reports `tag_dropped`.
- A salt that exists but cannot be read is never regenerated.

Method and evidence:

- Method: Hypothesis over append, compact, load, clock and salt sequences,
  plus fault-injected input and output.
- Artefacts: `tests/test_outcome_records.py`, `tests/test_outcome_store.py`,
  `tests/test_outcome_store_salt.py` and `tests/test_outcome_store_writes.py`.
- Non-vacuity: the generator reaches the bound, an expiry and a torn tail.
- The writer tests cross the 12 MiB high-water mark, verify byte-budget
  eviction, reject an oversized record, and preserve the 16 MiB hard bound when
  compaction fails.
- Fault injection:
  `test_failed_append_rollback_is_repaired_before_the_next_append` writes half
  a record, makes `ftruncate` fail, then requires a later append to leave only
  complete, decodable rows. The test failed with `corrupt` before D35's repair
  and passes with it.

O9. Restart baseline. After a restart, the first window in the new generation
is attributed against the reading that exposed the restart. That reading is
never itself attributed.

- Method: a unit test and the systemd scenario.
- Non-vacuity: the scenario's counters after the restart exceed the old
  values, so a decrease-only rule would mislabel.
- Mutation: keeping the old baseline must fail the scenario.

O10. Key normalization. The key handles:

- registry origins, including pre-release versions with hyphens;
- git and path origins;
- a non-default `CARGO_HOME` and missing variables;
- both `--crate-name=foo` and `--crate-name foo`;
- `--target` as a name or as a JSON path.

- Method: parameterized tests.
- Non-vacuity: a row in which `CARGO_PKG_VERSION` disagrees with the
  directory name must become `path:`, never `registry:`.

O11. Totality (C-ISOLATE, C-TOKEN). An exception injected into each shell entry
point leaves `loop.failures == 0` and `admission.healthy`. Every token is
returned, and a graceful stop completes.

- Artefact: `tests/test_outcome_totality.py`.
- Mutation: removing the wrapper must make the test fail.

O12. Sighting soundness (H2 input). An entry is marked `seen` only when one
sample, taken while the entry was running, contained a full-emit process whose
`-C metadata` equals the entry's own. A dep-info-only process never marks an
entry. An entry without metadata is always `unavailable`. `not_seen` means at
least one sample and no match. `child_samples` counts exactly the samples taken
while the entry was running.

- Method:
  - parameterized tests of `request_metadata` and `full_emit_metadata` over
    each `-C`/`--codegen` spelling, each `--emit` form, and no `--emit`;
  - a Hypothesis property over generated launch, end and sample sequences,
    with overlapping groups of the same crate but different metadata.
- Artefacts: `tests/test_outcome_sightings.py` and
  `tests/test_outcome_sightings_properties.py`.
- Non-vacuity: `hypothesis.event` must classify `seen`, `not_seen` and
  `unavailable`, plus a sample that held a dep-info pass only.
- Mutations: each of these must fail a named test:
  - treating `--emit=dep-info` as full emit;
  - matching on the crate name instead of the metadata;
  - counting samples taken after the entry ended.

O13. Replay verdict boundaries. For H2 and H3 in W1, and H4 and H5 in EP-M9,
the verdict changes exactly at the thresholds fixed in ADR 004. For H2 the
boundaries are 95% and 90% agreement, 100 evaluable records and 2,000
attributed outcomes. The evaluable set is exactly as D25 defines it.

- Method: parameterized synthetic stores at, just above and just below each
  boundary, plus a test that pins the constants to ADR 004's text.
- Artefacts: `tests/test_outcome_replay_h2.py`,
  `tests/test_outcome_replay.py`.
- Non-vacuity: every verdict token is reached for every hypothesis that can
  produce it.
- Mutation: off-by-one on any boundary, or counting a key that holds only
  one outcome as evaluable, must fail.

Residual gaps:

- Label soundness on the real server rests on A1 to A3. EP-M1 checks a few
  hundred windows on the real binary, and the model server covers the
  interleavings.
- The RFC's caveat stands: labels from solo and unanimous windows favour
  quiet, uniform periods. On this host that means at most about 4.5% of cached
  compiles (S5).
- H2 is evaluated only against those same labels. Its agreement measures the
  classifier on quiet windows, while its use would be on busy ones. A sample
  every 0.25 s also misses full-emit children that live less than that, which
  biases the sighting clue towards long misses (R9). The H2 report states both
  limits.

## Plan of work

The design follows the repository's functional core and imperative shell
(developer's guide §3) and the hexagonal rule that the domain owns its ports.
Every new domain module uses the `outcome_` prefix, including the dedicated
Rust parser predictor. The module inventory gains a row for each new module.
The draft name `cache_outcome.attribute` becomes
`outcome_attribution.attribute`.

Domain modules are pure: no input or output, no `os`, `socket`, `subprocess` or
`threading`, and no clock.

- `outcome_stats.py`: `StatsReading` and `parse_statistics(text, supported)`.
- `outcome_attribution.py`: `FOOTPRINTS` (Table A as data),
  `attribute(before, after, window)`, and `confirm` (D12).
- `outcome_gate.py`: `QuietInputs` and `quiet_point(inputs)`.
- `outcome_key.py`: frozen `Salt`, `ToolchainIdentity` and `CacheKey` values,
  plus `crate_name`, `crate_origin`, `target`, `argument_digest` and
  `toolchain_identity`. The helpers receive the complete request argv, with the
  compiler at index 0, the request environment and `Salt` as values.
  `crate_origin` returns no value only when `CARGO_MANIFEST_DIR` is absent;
  otherwise it produces the RFC's registry, git or HMAC-keyed path form.
  `target` returns `host` when absent, preserves a target name, and HMACs a
  JSON target path. `argument_digest` omits argv[0], applies sccache 0.14's
  removals and cfg ordering, and includes its filtered, sorted `CARGO_*`
  environment. `toolchain_identity` HMACs the compiler argument and
  `RUSTUP_TOOLCHAIN`, and includes `CACHE_VERSION` and server version. The later
  `outcome_ports` module re-exports `Salt` rather than owning a second
  definition. `outcome_key_arguments.py` owns canonical argv normalization and
  Cargo environment selection for the argument digest.
- `json_values.py`: pure JSON object and non-negative integer validators for
  the statistics, D-Bus property and outcome-store boundaries. Callers retain
  responsibility for their vocabulary and error policy.
- `outcome_parser.py` (EP-M3): `ParserClass` and `parser_class(argv)`, a pure
  sccache 0.14 Rust-parser predictor used only for H3 evidence. `argv` is the
  complete request vector; the predictor applies the pinned parser rules to
  argv[1:]. It returns one closed-vocabulary class, never stores arguments and
  never affects admission.
- `outcome_records.py`: `OutcomeRecord` (including the bounded `parser_class`,
  `child_sighting` and `child_samples` values), `WindowSummary` (window size,
  the two refusal-counter deltas, and predicted counts for all three parser
  classes), `StoreHeader`, and pure `compact`. Records carry a `CacheKey`,
  result, origin class, runtime, observation time and bounded H3/H2 fields.
  `compact` receives time and clock-skew status as values; it applies D17's
  age, count and byte limits. It re-exports the codec API for compatibility.
- `outcome_record_format.py`: owns the typed record values and format-specific
  field validation. `outcome_record_codec.py` owns strict JSON Lines
  `encode_line`/`decode_lines`; `outcome_records.py` is the public facade and
  sole compaction owner. These are format-specific boundaries, not a general
  JSON utility.
- `outcome_window.py`: `OutcomeWindow[P]`, a ledger generic over an opaque
  payload. It retains at most 64 launch payloads; adding a 65th marks overflow
  while keeping the retained tuple bounded. The recorder is its only owner.
- `outcome_sightings.py` (EP-M8): `request_metadata(argv)`,
  `full_emit_metadata(argv)` and `ChildSightings`, which is the per-entry
  sample count and sighting, folded by the recorder on each `Sampled` event.
- `outcome_vocabulary.py`: the metrics `typ.Literal` aliases (`CacheResult`,
  `OutcomeReason`, `StoreReason`, `EvaluationReason`, `GateReason`,
  `StoreOperation` and, from EP-M8, `ChildSighting`), each tested equal to its
  `metrics_schema` table. `ParserClass` is local to `outcome_parser.py` and is
  tested against its documented three-value vocabulary; it is not a metrics
  field.
- `outcome_ports.py`: `StatisticsPort`, `ServerProcessPort` and
  `OutcomeStorePort` (each a `typ.Protocol`), `SaltSource` (a `Callable`
  alias), and their result and failure types. The clock is `timing.Clock`,
  extended with an optional `wall` callable.
- `outcome_recorder_types.py` owns immutable event, command, output and
  typestate values. The private `outcome_recorder_outputs.py` module builds
  journal and store values from completed windows and terminal transition
  outputs. `outcome_recorder.py` owns the reducer; its private
  `outcome_recorder_reading.py` helper validates terminal readings, updates
  baselines and schedules retries. `outcome_recorder_driver.py` executes
  statistics-port commands and returns effects without publishing or saving
  them. These helpers are private to the outcome recorder and are not
  general-purpose abstractions.

The application service, `outcome_recorder.py`, is a typestate:

- `RecorderState` is one of the frozen dataclasses `Idle` (which holds the
  back-off), `Querying` and `Disabled`.
- A pure `advance(state, event) -> tuple[RecorderState, Outputs]` handles
  `LaunchFailed` alongside `Launched`, `Ended`, `GateOpen`, `PollResult` and
  `Stopping`.
- The loop emits `GateOpen` only when a baseline is missing or the bounded
  window has an ended launch; the reducer independently checks quiet-point
  inputs, in-flight exclusion and its minimum read interval.
- `Outputs` carries events, records, span transitions (`QueryStarted`,
  `QueryFinished`) and port commands (`StartQuery`, `CloseQuery`).
- A thin driver, `OutcomeRecorder`, executes the port commands. It emits and
  saves nothing itself.

The adapters form the shell.

- `nonblocking_child.py`: `NonBlockingChild` and `ChildReaper`. They are
  generic, and the only sanctioned way for the loop to run a helper process.
  - `subprocess.Popen` gets an absolute executable, resolved once at start.
  - It runs with `stdin=DEVNULL`, `stderr=DEVNULL` and stdout as a
    non-blocking pipe. It passes no `preexec_fn`, user, group, umask or
    process group, which keeps CPython's `posix_spawn` path.
  - Reads are capped at 64 KiB in total. At the deadline the child gets
    `SIGKILL` and its pipe is closed at once. The reaper polls
    `Popen.poll()`, with a bound of 4. When it is full, starting a child
    fails with `read_skipped`, a gate reason.
  - The one live pipe comes out of
    `descriptor_budget.DESCRIPTOR_RESERVE` (16), and the developer's guide
    says so.
- `outcome_queries.py`:
  - `SccacheStatistics(StatisticsPort)`, with the argv module constant
    `("--show-stats", "--stats-format", "json")` after the executable. Its
    environment is the supervisor's own `PATH`, `HOME` and
    `XDG_RUNTIME_DIR`, plus `SCCACHE_SERVER_UDS`.
  - `PeerServerProcess(ServerProcessPort)`: the D2 generation read
    (EP-M5), and from EP-M8 the D24 child sample. Both are synchronous and
    bounded.
- `outcome_store.py`:
  - `OutcomeStorePort` exposes non-blocking `append` and `results()` calls, plus
    bounded `close(timeout)`. `StoreWriter` implements the port on a writer
    thread; its bounded queue accepts appends, and it performs bounded loading
    and internal compaction. The loop drains writer results each tick.
  - `file_salt(path) -> Salt | StoreFailure` creates the key in a private
    `mkstemp` file and atomically installs it with a no-clobber hard link. It
    checks its length and never regenerates an unreadable salt. It runs on the
    writer thread at start. The loop receives the
    `Salt` through the result queue. Launches before it arrives have no key:
    their attribution is unchanged, and no record is written.
  - The store path resolver `store_path(env)` is shared with the replay
    CLI.
- `outcome_telemetry.py`: maps `Outputs` to `Metrics.emit` calls and to live
  `JournalTracer` spans, one per in-flight query.
- `supervisor_outcomes.py`: `OutcomeShell` and `NullOutcomeShell`. The shell
  reads the gate inputs, routes the hooks to the recorder, drains the writer's
  results, and wraps every entry point (D19). Its entry points are
  `launched(entry, group_id)`, `emptied(GroupEnding)`, `on_tick(now)`,
  `describe()` and `close(timeout)`.

Changes to existing modules:

- `supervisor.py`:
  - a field `outcomes`, built in `start` from settings (a null shell when
    recording is off);
  - a hook after `_apply(group_emptied)`;
  - `close(1.0)` after `requests.close_all()`.

  The emptied loop moves into `group_recovery.empty_groups(supervisor)` to free
  lines.
- `supervisor_placement.py`: `outcomes.launched` on the admitted branch,
  after the release.
- `supervisor_polling.py`: `outcomes.on_tick(now)` last in `poll_services`.
- `config.py`: `cache_outcomes: str = "off"`, a `CACHE_OUTCOMES` constant,
  and a `_RANGE_RULES` row that rejects `record` with `cache_mode = "direct"`.
- `metrics_schema.py`:
  - three events;
  - the vocabularies `cache_result`, `outcome_attribution` (`solo`,
    `unanimous`, `inferred`, `none`), `crate_origin`, `outcome_reason`,
    `store_reason`, `evaluation_reason`, `store_operation` and
    `child_sighting`;
  - the per-launch fields `child_sighting` and `child_samples` on
    `cache_outcome`, and the aggregate counters `child_samples_taken` and
    `child_sample_failures` on `outcome_reading`;
  - the counters `window_groups`, `readings`, `readings_skipped`,
    `readings_failed`, `store_records` and `store_discarded`.
- `metrics_report.py`: an optional `cache_outcomes` section. The coverage
  denominator is `request_admitted` records with `cache_path=cached`.
- `tracing_export.py`:
  - EP-M6 registers `statistics_read` (one span per statistics query),
    `outcome_store_load`, `outcome_store_save` and `outcome_store_compact`;
  - EP-M7 adds `admission_selection`; EP-M9 adds `prediction_evaluate`;
  - the broad failure categories `statistics_query`, `store_io` and `salt`;
  - the span outcome `completed`.

  Detailed reasons stay in metrics only. ADR 004 amends ADR 003's scope.
- `status.py` and `status_render.py`: an optional `cache_outcomes` object
  and one line, absent when recording is `off`.
- `pyproject.toml`: the marker `sccache_host_check`, the console script
  `build-limits-cache-replay`, Skylos entry points, Vulture `ignore_names` for
  the CrossHair contracts, and any duplication-gate allowances. Each is listed
  in developer's guide §12.
- `Makefile`: a `test-sccache-host` target. It is `.PHONY`, has `##` help
  text, sets `BUILD_LIMITS_SCCACHE_HOST_CHECK=1`, and runs
  `pytest -m sccache_host_check tests`. It must pass `mbake validate Makefile`.

Test support:

- `tests/fixtures/fake_sccache.py` gains a
  `client --show-stats --stats-format json` mode. Its server gains counters,
  scripted outcomes from `FAKE_SCCACHE_SCRIPT` (JSON lines such as
  `{"result": "hit"}`), a scripted statistics delay, and an `INVOCATION_ID`
  inherited from the unit. `tests/test_fake_sccache.py` gains matching tests.
- `tests/fake_rustc.py` (new): a scripted fake `rustc` that answers `-vV`,
  `--print=sysroot` and dep-info requests and writes outputs. It gets a row in
  developer's guide §11.2.
- `tests/outcome_doubles.py` (new): fake ports, the model server and
  `RecordingSpans`.
- `tests/sccache_host.py` (new): `sccache_host_marks()`, modelled on
  `systemd_user_marks`, with its own opt-in test.
- `tests/sccache_stats.py` (new): validates pinned counter footprints and
  writes redacted replay-window readings; it is test-only and has no runtime
  callers.

W2:

- `outcome_replay.py` (pure) computes:
  - the H2 verdict (EP-M8), using D25's classifier;
  - the H3 verdict, from solo rows and window summaries;
  - the H4 registry hit rate after the first observation;
  - the H5 flip rates, excluding identity changes;
  - a comparison by Brier score and calibration of a base-rate predictor, a
    last-outcome predictor, and a Beta estimate over the last eight outcomes
    with an origin prior.

  The thresholds are constants pinned to ADR 004. The verdict tokens are
  `supported`, `refuted`, `inconclusive` (between the support and falsification
  thresholds), `insufficient` and `not_collected`.
- `outcome_replay_cli.py` provides
  `build-limits-cache-replay [h2|h3|h4|h5|all] [--store PATH] [--json]`. The
  JSON output carries a `schema` key. Exit status is 0 for a report (a missing
  store reports `insufficient`), 1 for a `corrupt` or `version_mismatch` store
  (with the reason named), and 2 for bad options. `h3` ships in EP-M7, `h2` in
  EP-M8, and `h4`, `h5` and `all` in EP-M9. Coverage derived from the journal
  stays in `build-limits-metrics`, so the replay never queries the journal.

## Milestones and plateaus

Each milestone ends with the gates run sequentially by the `scrutineer`
subagent: `make check-fmt`, `make typecheck`, `make lint` and `make test`.
Documentation changes add `make markdownlint` and `make nixie`, and EP-M7 also
runs `make test-systemd-user`.

Commit at least once per milestone, with separate commits for the red, green
and refactor steps when each leaves the tree green. Use a strict `xfail` to
commit a red step. Add the module-inventory rows (developer's guide §4) in the
milestone that adds each module.

### EP-M0: decisions recorded before any data

- Outcome:
  - `docs/adr-004-cache-outcome-recording.md` (Status: Proposed; Accepted
    when EP-M8 merges) records D2 to D25, the fixed thresholds of Table B,
    the 2,000-outcome minimum, the vocabulary and the ADR 003 scope
    amendment. RFC 0002 links to it. This ExecPlan records review corrections
    D27 to D32.
  - `docs/roadmap.md` gains a rewritten preamble covering both RFCs, with
    references qualified as in "RFC 0002 G1". It also gains phase 3,
    "Record and evaluate cache outcomes", with step 3.1 (W1: EP-M1 to
    EP-M8) and step 3.2 (W2: EP-M9).
  - `docs/contents.md` indexes ADR 004.
  - `tests/fixtures/sccache_0_14_0_stats.json` is captured by one read-only
    `--show-stats --stats-format json` against the managed socket, with
    `cache_location` and `basedirs` redacted.
  - G0 (S5) is recorded.
- Acceptance: the documentation gates pass, and the thresholds match RFC
  0002 verbatim.
- Recovery: revert the commits.
- Compatibility decision: none.

### EP-M1 (prototype, 1.5-hour time-box, not blocking): real sccache behaves as read

- Outcome: `tests/test_sccache_real_binary.py`, marked `sccache_host_check`
  and skipped unless `BUILD_LIMITS_SCCACHE_HOST_CHECK=1` and
  `sccache --version` prints `sccache 0.14.0`. It carries
  `pytest.mark.timeout(600)`.
- Setup:
  - It starts a private server with `SCCACHE_SERVER_UDS` and `SCCACHE_DIR`
    in a temporary directory, `SCCACHE_IDLE_TIMEOUT=0` and
    `SCCACHE_NO_DAEMON=1`.
  - The server runs under `systemd-run --user --scope` when available, so
    `INVOCATION_ID` is checked too.
  - It drives the server with a `make_toolchain` tree whose `rustc` is
    `tests/fake_rustc.py`.
- Checks:
  - A cold request moves the miss footprint, and a warm identical one moves
    only `cache_hits` (A1). The client's output and exit status are
    identical in both cases (C2).
  - `-vV` moves `requests_not_cacheable`, and `--print=sysroot` moves
    `requests_not_compile` (A3).
  - Across 200 exits, a reading taken immediately after each exit shows the
    whole footprint (A2).
  - `SO_PEERCRED` on the private socket yields the server's PID (A4).
  - During a cold request, the fake `rustc` sleeps for
    `FAKE_RUSTC_EMIT_SECONDS = 1.0` on full emit. A sample of the server's
    control group then finds a process whose argv carries the request's
    `-C metadata` and a full `--emit`. During a warm request it finds at most a
    `--emit=dep-info` process (A9, used by EP-M8).
- Fixture: the readings and true outcomes are saved as
  `tests/fixtures/sccache_0_14_0_windows.jsonl`. EP-M2 replays them through
  `attribute()` in continuous integration, which gives H1's label agreement.
- Go or no-go: go if every assertion passes. Otherwise take D9's fallback,
  document the operator procedure in EP-M7, and make D12 mandatory.
- Recovery: the check is additive and opt-in, so delete it to discard.
- Compatibility decision: none.

### EP-M2: pure statistics, attribution and gate

- Outcome: `outcome_vocabulary.py`, `outcome_stats.py`,
  `outcome_attribution.py` and `outcome_gate.py`. This discharges O1 and the
  gate half of C-NOSTART.
- Red:
  - `tests/test_outcome_stats.py`: the fixture, and empty, partial,
    malformed and unsupported-version documents.
  - `tests/test_outcome_attribution.py`.
  - `tests/test_outcome_gate.py`: each clause false in turn.
  - The CrossHair contract.
  - The BDD feature below.

  Run the command below, and expect
  `ModuleNotFoundError: No module named 'buildlimits.outcome_attribution'`:

  ```bash
  uv run pytest tests/test_outcome_stats.py tests/test_outcome_attribution.py \
    tests/test_outcome_gate.py tests/test_outcome_rules_behaviour.py
  ```

- Feature `tests/features/outcome_rules.feature`, with steps in
  `tests/test_outcome_rules_behaviour.py`:

  ```gherkin
  Feature: Attributing sccache outcomes from statistics at quiet points
    A label is given only when the counters can say which request it belongs to.

    Scenario: A solo window that only moved cache_hits is a hit
      Given a baseline reading in generation "a"
      And one cached group that ended cleanly
      When a reading in generation "a" shows cache_hits up by 1 and compile_requests up by 1
      Then the group is labelled "hit" with attribution "solo"

    Scenario: A solo window that only moved compilations is not a hit
      Given a baseline reading in generation "a"
      And one cached group that ended cleanly
      When a reading in generation "a" shows compilations up by 1 and compile_requests up by 1
      Then the group is labelled "not_cached" with attribution "solo"

    Scenario: A mixed window is never split
      Given a baseline reading in generation "a"
      And three cached groups that ended cleanly
      When a reading shows one hit, one miss and one declined request
      Then every group is "unattributed" with attribution "none"
      And the outcome reason is "mixed_window"

    Scenario: A restart is caught even when counters climb past the old values
      Given a baseline reading in generation "a" with compile_requests 5
      And one cached group that ended cleanly
      When a reading in generation "b" shows compile_requests 9
      Then the group is "unattributed" with outcome reason "generation_changed"

    Scenario: A signalled group makes its window ineligible
      Given a baseline reading in generation "a"
      And one cached group that was signalled
      When a reading in generation "a" shows a miss
      Then the group is "unattributed" with outcome reason "ineligible_window"

    Scenario: Cargo's version probe is declined, not a non-cacheable compile
      Given a baseline reading in generation "a"
      And one cached group that ended cleanly
      When a reading shows requests_not_cacheable up by 1 and compile_requests up by 1
      Then the group is labelled "declined" with attribution "solo"
  ```

- Conformance check: `tests/test_outcome_boundaries.py` pins the purity of
  the domain modules.
- Remaining gaps: keys, the recorder, the adapters and the wiring.
- Compatibility decision: none.

### EP-M3: keys, records and the window ledger

- Outcome: `json_values.py`, `outcome_key.py`, `outcome_key_arguments.py`,
  `outcome_parser.py`, `outcome_record_format.py`, `outcome_record_codec.py`,
  `outcome_records.py` and `outcome_window.py`. This discharges O7, the pure
  part of O8, and O10.
- Red: `tests/test_outcome_key.py`, `tests/test_outcome_key_properties.py`,
  `tests/test_outcome_parser.py`, `tests/test_outcome_records.py` and
  `tests/test_outcome_window.py`. Parser-class tests assert that metadata-only
  is cacheable with required fields and a supported crate type; dep-info-only,
  asm-only and `--emit=` predict `not_compile`; and `--emit=link,asm` predicts
  `declined`. Cover every parser refusal branch: parse errors, `-`, `-o`,
  `--sysroot`, repeated `--emit`, unsupported crate types, missing
  `-C extra-filename`, incremental compilation, an unsure target, multiple
  inputs, missing required fields, no supported crate type and unsupported emit
  kinds. Cover each `NotCompilation` option and both argv orders for a
  `NotCompilation` option paired with a `CannotCache` option.
- Add module inventory rows for these modules in developer's guide §4;
  document the parser's source version, in-memory-only argv handling and
  H3-only reuse in §9 when the integrated feature documentation lands in EP-M7.
- Snapshot: `tests/__snapshots__/test_outcome_records.ambr` pins one header
  line, one record line and one window-summary line under a fixed salt and
  fixed times, with a round-trip semantic assertion beside it.
- Compatibility decision: none; format version 1 is new.

### EP-M4: the reading sequencer over fake ports

- Outcome: `outcome_ports.py`, `outcome_recorder.py`, the private terminal
  reading helper `outcome_recorder_reading.py`, and the command driver
  `outcome_recorder_driver.py`. This discharges O2, O3, the reading reasons of
  O6, and O9 at unit level. D12's `confirm` stays behind a flag.
- Red: `tests/test_outcome_recorder.py` (examples) and
  `tests/test_outcome_recorder_machine.py`.
- Remaining gaps: the real adapters and the wiring.
- Compatibility decision: none.

### EP-M5: adapters

- Outcome: `nonblocking_child.py`, `outcome_queries.py`, `outcome_store.py`,
  `outcome_store_io.py` and `outcome_store_salt.py`. This discharges O5 at
  adapter level, the input and output part of O8, and the port half of O6.
- Red:
  - `tests/test_nonblocking_child.py`:
    - a slow script is killed at the deadline, reaped, and its pipe closed;
    - output is capped at 64 KiB;
    - the reaper bound holds;
    - the `Popen` keyword arguments are pinned.
  - `tests/test_outcome_queries.py`:
    - the extended `fake_sccache.py client --show-stats`, with the argv
      pinned;
    - the peer generation over a real listening socket, including a refused
      connect, `/proc` read failures, and a listener whose environment holds
      `INVOCATION_ID`.
  - `tests/test_outcome_store.py`, with a temporary `XDG_STATE_HOME`:
    - file modes;
    - a torn tail and a file over 16 MiB;
    - a version mismatch, and moving the file aside;
    - refused writes and a full queue;
    - a writer stalled for 5 seconds against the 1-second close bound;
    - salt creation, an unreadable salt and a changed salt.
- `tests/test_outcome_store_writes.py` covers partial append failures, failed
  rollback, repair before a later append, compaction retry and bounded close.
- Compatibility decision: none.

### EP-M6: inert schema, configuration and vocabulary (releasable plateau)

- Outcome:
  - `config.py` accepts `cache_outcomes`. With `record`, start-up logs
    "cache outcome recording is not available in this build" once.
  - `metrics_schema.py` gains the three events and their fields, and
    `tracing_export.py` the operations and categories.
  - `metrics_report.py` gains the optional section, which is empty without
    events.
  - A report snapshot from synthetic records.
  - The users' guide Table 2 gains the key.
- Red:
  - `tests/test_outcome_config.py`.
  - `tests/test_outcome_metrics.py`.
  - The O6 meta-test.
  - An extended `tests/test_metrics_records_compatibility.py`. A
    `cache_outcome` with an unknown `cache_result` must still decode its
    other fields, and `attributed` must be computed from
    `outcome_attribution`.
- Compatibility decision: D18. This release is compatible with the role's
  rendered configuration on a downgrade to it.

### EP-M7: wiring, status, report, `h3` and documentation

- Outcome:
  - `outcome_telemetry.py` and `supervisor_outcomes.py`;
  - the hooks (D19) and the moved emptied loop;
  - `OutcomeShell.on_tick` polls the child reaper, and `close(timeout)` cancels
    any outstanding helper, closes its pipe and reaps it within the close bound;
  - the status object and its line;
  - `outcome_replay.py` (H3 only) and `outcome_replay_cli.py h3`;
  - the documentation set below.
- Red:
  - `tests/test_outcome_loop.py` (O5);
  - `tests/test_outcome_noninterference.py` (O4);
  - `tests/test_outcome_totality.py` (O11);
  - `tests/test_outcome_null_shell.py` (C-DEFAULT), which spies on
    `subprocess.Popen`, `open`, `socket.socket` and `threading.Thread`;
  - status snapshots with and without the object;
  - `tests/test_replay_cli.py` (the `h3` exit codes);
  - `tests/test_cache_outcomes_systemd.py`, with
    `tests/features/cache_outcomes_on_systemd.feature` below.
- Opt-in performance test: `tests/test_outcome_latency.py`, marked
  `sccache_host_check`. It records p99 tick lengthening and append-enqueue time
  into `Outcomes & retrospective`, against the performance tolerance.
- Feature, marked `systemd_user`:

  ```gherkin
  Feature: Recording cache outcomes under a real user manager
    Background:
      Given an isolated supervisor with cache_outcomes "record" and a scripted fake sccache

    Scenario: Labels follow the script
      Given the fake sccache scripts "miss" then "hit"
      When two cached compiles run one after the other with nothing queued
      Then the journal has cache_outcome events "miss" then "hit" with attribution "solo"
      And the outcome store holds two records with the same key

    Scenario: No query runs while the restart barrier exists
      Given the cache recovery barrier is present
      When a cached compile finishes
      Then no statistics query was started

    Scenario: The first window after a restart uses the new generation
      Given one attributed compile in the first invocation
      When the cache unit restarts and its counters climb past the old values
      And one cached compile runs
      Then that compile is attributed against the new generation

    Scenario: A stalled statistics query does not stall admission
      Given the fake sccache answers statistics after 5 seconds
      When a cached compile finishes and another compile is submitted
      Then the second compile is admitted while the query is outstanding
      And an outcome_reading event reports "timeout"

    Scenario: Status shows that recording is on
      When a cached compile is attributed
      Then build-limits-status shows a cache outcomes line with 1 attributed
  ```

- Documentation:
  - `docs/users-guide.md`:
    - "Supervisor settings": the `cache_outcomes` key, and that `record`
      conflicts with `cache_mode = "direct"`.
    - A new section, "Record cache outcomes", covering:
      - what is recorded and what is never recorded;
      - the store path, its file modes, how to delete it and what that
        costs;
      - the status line;
      - `build-limits-metrics --max-records 100000`;
      - `build-limits-cache-replay h3`;
      - the upgrade order (every journal reader first);
      - the downgrade rule (remove the key first);
      - the sccache-upgrade runbook: on `unsupported_version`, run
        `make test-sccache-host`, then raise `SUPPORTED_SCCACHE_VERSIONS`;
      - D9's operator procedure, if that fallback was taken.
    - "Metrics" and "Status": the new section and the new line.
  - `docs/developers-guide.md`:
    - §4: the module rows.
    - §6: the outcome shell, the hook placement, the writer thread, and the
      `nonblocking_child` convention (the only way the loop runs a helper
      process).
    - §9: the recorder, the ports and their reasons, and the events and
      spans.
    - §11.2: the helpers. §11.3: the new state machines and the CrossHair
      contract. §11.4: `make test-sccache-host`.
    - §12: the Skylos, Vulture and duplication entries.
  - `docs/contents.md` is updated.
- Conformance check: O4 and O11 pass, `supervisor.py` stays at 400 lines or
  fewer, and no admission file has changed.
- Recovery: with the key off, the null shell restores today's behaviour.
- Compatibility decision: none beyond EP-M6.

### EP-M8: H2 child sightings (W1, added by the owner's Q1 answer)

- Outcome:
  - `outcome_sightings.py` (pure): `request_metadata`, `full_emit_metadata`
    and `ChildSightings`.
  - The recorder gains a `Sampled(metadata: frozenset[str])` event and a
    `SampleFailed` event. The pure `advance` folds each sample into the
    running entries (O12).
  - `PeerServerProcess.full_emit_metadata()` implements D24: the server's
    control group is cached per generation, at most 64 processes are
    inspected, and argv is classified once per `(pid, starttime)`.
  - `OutcomeShell.on_tick` takes a sample every `CHILD_SAMPLE_SECONDS`
    while recording is on and `cached_moving > 0`. It is total (D19), and
    failures back off.
  - Schema:
    - the `child_sighting` vocabulary (`seen`, `not_seen`, `ambiguous`,
      `unavailable`);
    - the `child_samples` counter on `cache_outcome`;
    - the aggregate counters `child_samples_taken` and
      `child_sample_failures` on `outcome_reading`.
  - The store record gains `child_sighting` and `child_samples`. Record
    format version 1 is finalized in this milestone. Nothing is released with
    recording enabled between EP-M3 and here (only EP-M6, the inert plateau,
    can be released), so no deployed store needs migrating.
  - `outcome_replay` gains H2 (D25, D31), and the CLI gains `h2`.
  - The `cache_outcomes` report section gains counts by `child_sighting`.
- Red:
  - `tests/test_outcome_sightings.py` and
    `tests/test_outcome_sightings_properties.py` (O12), including duplicate
    active metadata: one child must never mark multiple entries `seen`.
  - `tests/test_outcome_server_process.py`. A fake `/proc` and cgroup tree
    under `tmp_path` checks the 64-process bound, the per-`(pid, starttime)`
    cache, a process that exits mid-sample (A10), an unreadable group, and a
    PID reused with a new start time.
  - `tests/test_outcome_replay_h2.py` (O13).
  - An extended `RecorderMachine` with sample rules. Its non-vacuity labels
    gain `seen`, `not_seen` and `ambiguous`; H2 child-only agreement excludes
    ambiguous samples while the combined classifier may still use runtime.
  - A sixth systemd scenario, added to
    `tests/features/cache_outcomes_on_systemd.feature`:

    ```gherkin
    Scenario: Child sightings follow the script
      Given the fake sccache scripts "miss" with a compiler that runs for 1 second, then "hit"
      When two cached compiles with distinct -C metadata run one after the other with nothing queued
      Then the "miss" cache_outcome reports child_sighting "seen"
      And the "hit" cache_outcome reports child_sighting "not_seen"
      And the outcome store holds no argument or metadata value
    ```

    The fake server already runs a miss's compiler as its own child. For a
    scripted `hit` it replies without running one.

    A second scenario holds two active requests with the same metadata while
    one runs a full-emit child. Both entries report `ambiguous`, and H2 does
    not use that child as evidence for either request.
  - A snapshot of `build-limits-cache-replay h2` text and JSON over a fixture
    store, paired with semantic assertions on the verdict and the sample
    sizes.
- Opt-in performance: `tests/test_outcome_latency.py` adds the p99 cost of a
  sample against the tolerance.
- Documentation:
  - `docs/users-guide.md`, "Record cache outcomes": what a child sighting
    is, how shared metadata produces an ambiguous sighting, that only a token
    and a count are stored, how sampling cost is bounded,
    `build-limits-cache-replay h2` and its limits.
  - `docs/developers-guide.md` §6 and §9: the sample cadence, the
    server-process port, and the rule that `/proc` reads stay inside
    `PeerServerProcess`.
  - ADR 004 records D23 to D25 and becomes Accepted.
- Requirements: RFC-H2 and RFC-W1 (the H1 to H3 exit evidence), plus Q1 (b).
- Conformance check: O4, O11 and O12 hold. No argv, metadata or path reaches
  the store, the journal or a span (an extended O7). Sampling never runs when
  recording is `off` (the C-DEFAULT spy test is extended to `/proc` opens).
- Recovery: the sampling is additive behind the same key.
- Remaining gaps: using `inferred` outcomes if H2 survives is out of scope.
- Compatibility decision: none. The record format is still unreleased, and
  the new metric fields are additive (old readers omit them).

### G1 (owner gate, runbook)

1. Release EP-M8 (W1 complete) by tag and install it through the
   dev-env-rocky role, upgrading every journal reader first. Do not enable
   recording on a release that lacks EP-M8.
2. Set `cache_outcomes = "record"` and restart the supervisor gracefully.
3. After 24 hours, check the status line and run
   `build-limits-cache-replay h3` and `build-limits-cache-replay h2`. If
   nothing has been attributed, look at the last reading reason and fix it
   before going on. If `child_sighting` is `unavailable` for nearly every
   record, look at `child_sample_failures` in the `cache_outcomes` report.
4. Track progress by the attributed count on the status line, not by journal
   windows.
5. When 2,000 attributed outcomes exist, or after 14 days, whichever comes
   first, record the evidence:
   - the commit, the sccache version and the date range;
   - the totals;
   - the coverage by origin, from
     `build-limits-metrics --since 14d --max-records 100000`;
   - the H1 verdict, with label agreement taken from EP-M1's fixture
     replay;
   - the H2 verdict, with its agreement, evaluable count, the share of
     misses that were `seen`, and its stated limits;
   - the H3 verdict.
6. Record the decision in `Decision log`:
   - `proceed` moves the plan to `IN PROGRESS` and starts EP-M9;
   - `narrow` starts EP-M9 with the limits recorded;
   - `stop` means: set the key to `off`, delete `~/.local/state/buildlimits/`,
     and plan removal of the code in a separate change.

   Independently of the overall decision, H2's verdict decides sampling:
   - `refuted` means planning removal of sampling, keeping wall time (RFC H2);
   - `supported` lets a separate plan propose `inferred` outcomes, which are
     reported apart from labels (RFC I4);
   - `inconclusive` or `insufficient` means continuing to record and
     re-running `h2` later.

   The status stays `BLOCKED` until then, and only the owner changes it.

### EP-M9: W2 offline replay (after G1 says proceed or narrow)

- Outcome: `h4`, `h5` and `all` in the replay CLI. They report verdicts,
  sample sizes, the time span the store covers (R8), and the quiet-window
  caveat. Below 2,000 attributed records, the verdict is `insufficient`.
- Red:
  - `tests/test_outcome_replay.py`: synthetic sets at, above and below each
    threshold.
  - A Hypothesis property that verdicts do not depend on record order within
    a key.
  - Snapshots of the text and JSON output, each paired with a semantic
    assertion.
  - End-to-end runs of the console script.
- Compatibility decision: none.

## Concrete steps

Work from
`/home/leynos/.lody/repos/github---leynos---buildlimits/worktrees/ac9f6ac4-ab16-4230-a4f9-0b19d16fbf25`.

1. Run `git branch --show-current` and expect `cache-hit-prediction`.
2. For each milestone, write the red tests, then run only them through the
   Makefile's environment, capturing a log:

   ```bash
   uv run pytest <new test files> 2>&1 | tee /tmp/red-buildlimits-cache-hit-prediction.out
   ```

   Expect only the stated failure. For a new module this is
   `ModuleNotFoundError: No module named 'buildlimits.<module>'`. For new
   behaviour in an existing module, a strict `xfail` reports `XFAIL`.
3. Implement, then rerun the same command and expect every test to pass.
   Remove any `xfail` marker in the green commit.
4. Ask `scrutineer` to run, in order: `make check-fmt`, `make typecheck`,
   `make lint` and `make test`. Add `make markdownlint` and `make nixie` for
   documentation, and `make test-systemd-user` in EP-M7. Each gate's log goes to
   `/tmp/<gate>-buildlimits-cache-hit-prediction.out`.
5. Commit with an imperative subject of about 50 characters and the
   session's attribution trailer. Update `Progress` with a UTC timestamp.
6. At each milestone boundary, write the conformance check and any mutation
   results into `Outcomes & retrospective`.

## Validation and acceptance

- Tests:
  - `make test` passes, and every new test existed red first, with its log
    kept.
  - `make test-systemd-user` passes the six new scenarios.
  - `make test-sccache-host` passes on a host with sccache 0.14.0, or the D9
    fallback is recorded.
- Verification: O1 to O11 are discharged, with their non-vacuity evidence.
  Each mutation control was run once as a temporary, uncommitted edit and
  failed its named test, and the results are recorded in this plan.
- Lint and typecheck: `make lint` and `make typecheck` pass. Every new
  suppression links to its planned fix.
- Performance: the opt-in latency figures are recorded and within
  tolerance.
- Behaviour: on the isolated harness with `record`, the status line, the
  `cache_outcomes` report section and `build-limits-cache-replay h3` all agree
  with the fake sccache's script.

## Idempotence and recovery

Every milestone is additive until the hooks in EP-M7, and rerunning tests is
safe. Stores live under each test's own `XDG_STATE_HOME`, and the systemd
scenarios use uniquely named units that the harness removes.

To roll back on a host, set `cache_outcomes = "off"` and restart gracefully. To
downgrade past EP-M6, remove the key first. Deleting
`~/.local/state/buildlimits/` removes every trace of the store; the only cost
is the replay.

## Artefacts and notes

Table A gives the countermovement per request at sccache 0.14.0, from RFC 0002
§1 and S1. Every row also moves `compile_requests` by 1. The column
abbreviations are:

- `hits`: `cache_hits`;
- `comp`: `compilations`;
- `miss`: `cache_misses`;
- `ncc`: `non_cacheable_compilations`;
- `fail`: `compile_fails`;
- `err`: `cache_errors`;
- `exec`: `requests_executed`;
- `ncr`: `requests_not_cacheable`;
- `nco`: `requests_not_compile`;
- `uns`: `requests_unsupported_compiler`.

| Footprint      | hits | comp | miss | ncc | fail | err | exec | ncr | nco | uns | Label           |
| -------------- | ---- | ---- | ---- | --- | ---- | --- | ---- | --- | --- | --- | --------------- |
| hit            | 1    | 0    | 0    | 0   | 0    | 0   | 1    | 0   | 0   | 0   | `hit`           |
| miss           | 0    | 1    | 1    | 0   | 0    | 0   | 1    | 0   | 0   | 0   | `miss`          |
| miss, read err | 0    | 1    | 1    | 0   | 0    | 1   | 1    | 0   | 0   | 0   | `miss`          |
| not cached     | 0    | 1    | 0    | 0   | 0    | 0   | 1    | 0   | 0   | 0   | `not_cached`    |
| not cacheable  | 0    | 1    | 0    | 1   | 0    | 0   | 1    | 0   | 0   | 0   | `not_cacheable` |
| failed         | 0    | 1    | 0    | 0   | 1    | 0   | 1    | 0   | 0   | 0   | `failed`        |
| error (fails)  | 0    | 0    | 0    | 0   | 1    | 0   | 1    | 0   | 0   | 0   | `error`         |
| error (cache)  | 0    | 0    | 0    | 0   | 0    | 1   | 1    | 0   | 0   | 0   | `error`         |
| fatal error    | 0    | 0    | 0    | 0   | 0    | 1   | 1    | 0   | 0   | 0   | `error`         |
| silent         | 0    | 0    | 0    | 0   | 0    | 0   | 1    | 0   | 0   | 0   | none            |
| declined       | 0    | 0    | 0    | 0   | 0    | 0   | 0    | 1   | 0   | 0   | `declined`      |
| not compile    | 0    | 0    | 0    | 0   | 0    | 0   | 0    | 0   | 1   | 0   | `not_compile`   |
| unsupported    | 0    | 0    | 0    | 0   | 0    | 0   | 0    | 0   | 0   | 1   | `unsupported`   |

_Table A: Per-request counter footprints at sccache 0.14.0._

A solo window takes the label of its single footprint. A unanimous window of
_n_ launches is labelled only if its delta is either _n_ copies of the `hit`,
`not_cached`, `not_cacheable` or `declined` footprint, or _n_ misses with
anywhere from 0 to _n_ read errors. Every other balanced window is
`mixed_window`.

Table B gives the thresholds that EP-M0 fixes, verbatim from RFC 0002.

| Hypothesis | Prediction                                                                                                                 | Falsified if                     |
| ---------- | -------------------------------------------------------------------------------------------------------------------------- | -------------------------------- |
| H1         | No attributed label disagrees with truth in at least 200 controlled windows; host coverage at least 20% of cached requests | Any wrong label, or coverage <5% |
| H2         | Wall time plus a sighted child agrees with at least 95% of window labels                                                   | Below 90%                        |
| H3         | The sccache 0.14 Rust parser's first result is predicted; each refusal class moves only its matching counter               | One counterexample               |
| H4         | Registry crates at a pinned version with an unchanged argument digest hit at least 95% after their first observation       | Below 90%                        |
| H5         | At most 5% of consecutive outcome pairs within one key flip without a toolchain change                                     | Above 10%                        |

_Table B: Hypothesis thresholds (minimum 2,000 attributed outcomes)._

G0, the coverage bound, was measured read-only on 2026-10-23 with the command
below, giving the result in S5. The bound counts cached finishes with
`queued == 0`, `server_drain == 0` and `running + draining <= 1`.

```bash
journalctl --user BUILD_LIMITS_SCHEMA=1 BUILD_LIMITS_EVENT=request_finished \
  _SYSTEMD_USER_UNIT=buildlimits-supervisor.service --since -7d -o json \
  --output-fields=BUILD_LIMITS_CACHE_PATH,BUILD_LIMITS_QUEUED,BUILD_LIMITS_RUNNING \
  --output-fields=BUILD_LIMITS_DRAINING,BUILD_LIMITS_SERVER_DRAIN \
  > /tmp/g0-buildlimits-cache-hit-prediction.jsonl
```

The statistics document below is abbreviated to the fields that `outcome_stats`
reads; the EP-M0 fixture pins the full shape.

```json
{"stats": {"compile_requests": 3, "requests_unsupported_compiler": 0,
  "requests_not_compile": 0, "requests_not_cacheable": 1,
  "requests_executed": 2, "cache_errors": {"counts": {}, "adv_counts": {}},
  "cache_hits": {"counts": {"Rust": 1}, "adv_counts": {"rust": 1}},
  "cache_misses": {"counts": {"Rust": 1}, "adv_counts": {"rust": 1}},
  "non_cacheable_compilations": 0, "compilations": 1, "compile_fails": 0},
 "version": "0.14.0"}
```

## Interfaces and dependencies

These names must exist at the end of the milestone named in each comment.
Private helpers are omitted.

```python
# buildlimits/outcome_vocabulary.py (EP-M2); each alias equals its metrics_schema table
type CacheResult = typ.Literal["hit", "miss", "not_cacheable", "not_cached", "failed", "error",
                               "declined", "not_compile", "unsupported", "unattributed"]
type OutcomeReason = typ.Literal[
    "none", "timeout", "read_error", "parse_error", "unsupported_version",
    "generation_unavailable", "generation_changed", "empty_stats",
    "counter_decreased", "ineligible_window", "count_mismatch", "mixed_window",
    "straddled_reading", "supervisor_stopped", "no_baseline", "window_overflow",
    "recorder_disabled",
]
type StoreReason = typ.Literal[
    "none", "corrupt", "unreadable", "version_mismatch", "tag_dropped",
    "salt_unavailable", "save_failed", "expired", "salt_changed", "clock_skew",
]
type EvaluationReason = typ.Literal[
    "none", "key_unavailable", "store_unavailable", "ineligible",
]
type StoreOperation = typ.Literal["load", "append", "compact", "salt"]
type GateReason = typ.Literal["queued", "cached_moving", "server_drain", "unreachable", "held",
    "recovering", "barrier", "in_flight", "read_skipped", "backing_off"]
type ChildSighting = typ.Literal["seen", "not_seen", "ambiguous", "unavailable"] # EP-M8
type Verdict = typ.Literal["supported", "refuted", "inconclusive", "insufficient", "not_collected"]

# buildlimits/outcome_stats.py (EP-M2)
@dc.dataclass(frozen=True, slots=True)
class StatsReading:
    compile_requests: int
    requests_unsupported_compiler: int
    requests_not_compile: int
    requests_not_cacheable: int
    requests_executed: int
    cache_hits: int            # sum of PerLanguageCount.counts
    cache_misses: int
    cache_errors: int
    non_cacheable_compilations: int
    compilations: int
    compile_fails: int
    version: str

@dc.dataclass(frozen=True, slots=True)
class ReadFailure:
    reason: typ.Literal["parse_error", "unsupported_version"]

def parse_statistics(text: str, *, supported: frozenset[str]) -> StatsReading | ReadFailure: ...

# buildlimits/outcome_parser.py (EP-M3); argv is inspected, never retained
type ParserClass = typ.Literal["cacheable", "not_compile", "declined"]
def parser_class(argv: cabc.Sequence[str]) -> ParserClass: ...

# buildlimits/outcome_attribution.py (EP-M2)
@dc.dataclass(frozen=True, slots=True)
class Generation:
    invocation: str            # INVOCATION_ID or ""
    pid: int
    starttime: int

@dc.dataclass(frozen=True, slots=True)
class Reading:
    generation: Generation | None   # None: no server listening
    stats: StatsReading

@dc.dataclass(frozen=True, slots=True)
class WindowEntry:
    group_id: str
    is_clean: bool             # not Group.may_orphan_server_work

@dc.dataclass(frozen=True, slots=True)
class Attribution:
    labels: tuple[tuple[str, CacheResult], ...]
    kind: typ.Literal["solo", "unanimous", "none"]
    reason: OutcomeReason

def attribute(before: Reading, after: Reading, window: tuple[WindowEntry, ...]) -> Attribution: ...

# buildlimits/outcome_gate.py (EP-M2)
@dc.dataclass(frozen=True, slots=True)
class QuietInputs:
    counts: Counts
    is_reachable: bool
    is_cache_allowed: bool
    is_recovery_idle: bool
    has_barrier: bool
    has_query_in_flight: bool

def quiet_point(inputs: QuietInputs) -> GateReason | None: ...

# buildlimits/outcome_ports.py (EP-M4)
@dc.dataclass(frozen=True, slots=True)
class Ticket:                  # opaque; at most one outstanding per port
    serial: int
    deadline: float

@dc.dataclass(frozen=True, slots=True)
class Pending: ...

@dc.dataclass(frozen=True, slots=True)
class StatisticsText:
    text: str

@dc.dataclass(frozen=True, slots=True)
class QueryFailure:
    reason: typ.Literal["timeout", "read_error"]

@dc.dataclass(frozen=True, slots=True)
class GenerationFailure:
    reason: typ.Literal["generation_unavailable"]

@dc.dataclass(frozen=True, slots=True)
class StoreFailure:
    reason: StoreReason

@dc.dataclass(frozen=True, slots=True)
class StoreResult:
    operation: StoreOperation
    reason: StoreReason      # "none" for success
    records: int
    discarded: int

@dc.dataclass(frozen=True, slots=True)
class Salt:
    key: bytes                 # 32 bytes
    salt_id: str               # HMAC(key, "buildlimits-salt-id"), hex

class StatisticsPort(typ.Protocol):
    def start(self, now: float) -> Ticket | QueryFailure: ...
    def poll(self, ticket: Ticket, now: float) -> Pending | StatisticsText | QueryFailure: ...
    def close(self, ticket: Ticket, /) -> None: ... # idempotent; poll after a terminal result is an error

@dc.dataclass(frozen=True, slots=True)
class SampleFailure:                                                           # EP-M8
    reason: typ.Literal["server_unknown", "group_unreadable"]

class ServerProcessPort(typ.Protocol):
    def generation(self) -> Generation | None | GenerationFailure: ...         # EP-M5
    def full_emit_metadata(self) -> frozenset[str] | SampleFailure: ...       # EP-M8; values never leave memory

# buildlimits/outcome_sightings.py (EP-M8)
def request_metadata(argv: cabc.Sequence[str]) -> str | None: ...
def full_emit_metadata(argv: cabc.Sequence[str]) -> str | None: ...

@dc.dataclass(frozen=True, slots=True)
class ChildSightings:
    samples: int
    is_seen: bool
    is_ambiguous: bool
    has_metadata: bool

    def sighting(self) -> ChildSighting: ...

class OutcomeStorePort(typ.Protocol):
    def append(self, line: OutcomeRecord | WindowSummary) -> None: ...  # non-blocking
    def results(self) -> tuple[StoreResult, ...]: ...                   # non-blocking
    def close(self, timeout: float) -> None: ...                        # bounded

type SaltSource = cabc.Callable[[], Salt | StoreFailure]
```

There are no new dependencies.

Load these skills while implementing, starting with `python-router`:

- `execplans`, to keep this plan current;
- `hexagonal-architecture`, for domain and port boundaries;
- `python-types-and-apis`, for Protocols, `Literal` aliases and the
  typestate;
- `python-data-shapes`, for frozen dataclasses;
- `python-concurrency`, for the writer thread and the non-blocking child;
- `python-errors-and-logging`, for failure alternatives instead of
  exceptions;
- `python-testing`, `hypothesis`, `crosshair` and `python-verification`,
  for O1 to O11;
- `python-quality-tools` and `skylos`, for the dead-code entries;
- `en-gb-oxendict-style`, for the documentation;
- `commit-message` and `pr-creation`.

Consult these documents:

- RFC 0002, every section;
- ADR 001, for graceful restart and status conventions;
- ADR 003, for tracing;
- `docs/developers-guide.md`, §§3 to 6, 9, 11 and 12;
- `docs/users-guide.md`: "Supervisor settings", "Status" and "Metrics";
- `docs/documentation-style-guide.md`, for the ADR and roadmap formats;
- `docs/scripting-standards.md`.

## Revision note

- 2026-10-23: the initial draft, from RFC 0002, Wyvern reconnaissance and
  Firecrawl checks.
- 2026-10-23: the second revision, after the six-member Logisphere panel and
  the G0 measurement.
  - What changed:
    - a spawn-free peer generation replaces `systemctl` (D2);
    - three events replace one event with two grains (D6);
    - an append-only store with a writer thread replaces whole-file saves on
      the loop (D17);
    - a pure toolchain identity replaces the filesystem fingerprint (D10);
    - the gate is pure and gains `queued == 0` (D20);
    - the H3 predicate is corrected and gains window evidence (D21);
    - `h3` moves into W1 (D22);
    - totality and hook placement are specified (D19);
    - a releasable inert plateau is added (D18), with back-off (D16) and
      new vocabulary (D15);
    - EP-M5 is split into EP-M4 to EP-M7, and W2 becomes EP-M8 (renumbered
      EP-M9 in the third revision);
    - Tables A and B are inlined, and Q1 is raised from G0.
  - Effect on the remaining work: its intent is unchanged. Next comes owner
    approval, then EP-M0.
- 2026-10-23: the third revision, after the owner answered Q1 with (b).
  - What changed:
    - EP-M8 is added to W1: H2 child sightings, the `h2` replay and their
      documentation;
    - W2 is renumbered EP-M9;
    - D23 is superseded, and D24 (the server-process port, still five ports)
      and D25 (the H2 design and classifier, fixed before data) are added;
    - axioms A9 and A10, obligations O12 and O13, and risk R9 are added;
    - EP-M1 gains a sighting check;
    - the G1 runbook releases EP-M8 and records H2;
    - ADR 004 now becomes Accepted with EP-M8.
  - Why: G0 bounds solo coverage near 4.5%, and the RFC makes H2 the route to
    coverage below 5%.
  - Effect on the remaining work: W1 grows by one milestone, and G1 waits for
    it. W2 is unchanged apart from its number.
- 2026-10-23: the owner approved the whole plan, including deviations D2 to
  D25. EP-M0 is authorized; EP-M9 remains gated by G1.
- 2026-10-24: CodeRabbit's EP-M0 review identified a long-run store-size gap
  and H3 classification errors. D27 to D29 record the first corrections,
  checked against sccache v0.14.0 source before editing.
- 2026-10-24: the fresh EP-M0 review found omitted parser refusal branches and
  shared child metadata that could be assigned to multiple entries. D30 to D32
  define the complete parser-order predictor, ambiguous child sightings and RFC
  footnote order; the pinned source and fixture were checked before editing.
- 2026-10-25: EP-M5's store writer and append-recovery test are complete.
  - What changed: added the store adapters, repaired the salt-installation
    description, and recorded candidate98's full gate and review evidence.
  - Why: the store writer completes EP-M5, and review exposed a partial-write
    rollback failure that had to be repaired before a later append.
  - Effect on the remaining work: EP-M5 is complete. EP-M6 is next; G1 still
    gates EP-M9, with no W1 or W2 acceptance criteria changed.
