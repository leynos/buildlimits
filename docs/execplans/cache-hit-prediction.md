# Record the real outcome of each cached compile and evaluate prediction offline

This ExecPlan (execution plan) is a living document. The sections `Constraints`,
`Tolerances`, `Risks`, `Progress`, `Surprises & discoveries`, `Decision log`,
`Outcomes & retrospective`, `Conformance basis`, and `Verification plan` must
be kept up to date as work proceeds.

Status: DRAFT

Branch: `cache-hit-prediction`. Plan path:
`docs/execplans/cache-hit-prediction.md`. Upstream: RFC 0002, accepted
2026-10-23 at `42f40e4`.

## Purpose / big picture

Nothing in buildlimits knows whether a compile was an sccache hit. A waiting
request in `build-limits-status` could be a 200-millisecond cache read or a
five-minute compile, and nobody can judge proposals to admit hits sooner
without knowing how often hits occur and whether they can be foreseen.

RFC 0002 answers this in layers, each gated on evidence from the one before.
Its acceptance approves workstreams W1 and W2 only:

- W1, recording: the supervisor reads the managed sccache server's global
  statistics at _quiet points_ and labels each cached compile `hit`, `miss`,
  and so on wherever the label can be soundly attributed to one request. The
  label is journalled as a closed-vocabulary metric, and a keyed record is
  stored locally for offline analysis. A real-binary check confirms the sccache
  facts the attribution relies on.
- W2, offline evaluation: a replay over W1's records yields the H2 to H5
  verdicts (whether a hit can be foreseen from history) without building any
  prediction into admission.

Layers 2 and 3 (W3 to W6) are out of scope. They proceed only when the owner's
gates G2 to G5 release them, and each needs its own plan.

After this plan's W1 milestones, an operator can set
`cache_outcomes = "record"` in `~/.config/buildlimits/supervisor.toml`, build
as normal, and run:

```bash
build-limits-metrics --since 1d
```

They will see a `cache_outcomes` section like the one below. The figures are
illustrative.

```plaintext
cache_outcomes: {"attributed": 812, "coverage": 0.41, "groups": 1980,
  "reasons": {"count_mismatch": 3, "ineligible_window": 41, "none": 812, ...},
  "results": {"hit": 530, "miss": 190, "refused": 77, ...}}
```

After the W2 milestone, and only once the owner has decided G1, the operator
runs `build-limits-cache-replay` over the local record store and reads the H2
to H5 verdicts against thresholds that were fixed before any data was read.
Admission, token accounting, `SERVER_DRAIN` retention and cache recovery behave
identically whether recording is on or off. That is the central safety claim,
and the plan proves it with a differential test.

## Constraints

These are hard invariants. Violating one requires escalation, not a workaround.

- C-OBS (RFC I1, observation only): no decision in `buildlimits/admission.py`,
  `admission_state.py`, `server_watch.py`, `cache_recovery.py`,
  `recovery_starvation.py`, `recovery_roots.py` or `recovery_outlook.py` may
  read an outcome, a record, a key or a reading. The import-boundary test in
  `tests/test_admission.py` (`_WIRE` table near line 357) grows to forbid every
  new module from those files.
- C-NOSTART (RFC I2): the supervisor runs only
  `sccache --show-stats --stats-format json`, never `--zero-stats`,
  `--dist-status` or `--start-server`. It never queries while a recovery hold
  is active, while the restart barrier exists, while the cache is unreachable,
  or while recovery is not idle.
- C-ISOLATE (RFC I3): a statistics, unit-property, store or salt failure
  yields a named reason, never an unhealthy supervisor, a refusal or a delayed
  admission. No call that this plan adds may block the selector loop for longer
  than one bounded, non-blocking poll.
- C-NOINFER (RFC I4): an attributed label is never inferred. W1 produces only
  `solo` and `unanimous` attributions. `inferred` is reserved for H2 and is
  never emitted by this plan.
- C-PRIV (RFC §2 privacy): no raw path, argument or environment value is
  written to the journal, a span or the store. Local paths and any digest that
  covers a path are keyed with the per-host salt. No salt means no record.
  Records never leave the host.
- C-DEFAULT: every new behaviour sits behind `cache_outcomes`, whose default
  is `off`. With `off`, the supervisor spawns no new process, opens no new file
  and emits no new event.
- C-SIZE: no code file exceeds 400 lines (AGENTS.md). `supervisor.py` (392
  lines), `server_watch.py` (392), `cache_recovery.py` (400) and `groups.py`
  (391) have no headroom. New wiring therefore lives in new modules, with
  one-line hooks in the existing ones.
- C-HOST: never stop, restart, signal or reconfigure the live
  `buildlimits-supervisor.service` or `buildlimits-sccache.service`. Never
  install on the host or run `build-limits-recover`. Never bypass compile
  admission: no test or tool in this plan runs a real `rustc` outside an
  admitted scope (see the W1 host check design).
- C-PY: Python 3.14 baseline. The tools are those pinned in `pyproject.toml`
  and `Makefile`. No new runtime dependency. The standard library's `hashlib`,
  `hmac`, `secrets`, `json` and `subprocess` suffice.
- C-SCHEMA: `cache_outcome` and `outcome_store` must be in
  `metrics_schema.EVENTS` in the same release that can emit them (RFC
  "Compatibility and migration").
- C-GATES: the plan must not begin W2 implementation until the owner has
  recorded the G1 decision (`proceed` or `narrow`) in this plan's
  `Decision log`. W3 and later are out of scope.

## Tolerances (exception triggers)

- Scope: stop and escalate if W1 needs more than 30 changed source files
  (excluding tests and docs) or more than 3,000 net added lines of production
  code.
- Interface: stop and escalate before changing the wire protocol
  (`protocol.py`), the status document's existing keys, the effect types in
  `effects.py`, or any existing metric event's fields.
- Admission: stop and escalate if any milestone appears to need a change to
  `admission.py` or `admission_state.py` beyond the read-only accessors that
  `Gauges` already offers.
- Dependencies: stop and escalate if a new runtime or development dependency
  seems necessary.
- Iterations: stop and escalate if a gate still fails after three fix
  attempts on the same cause.
- Feasibility: stop and escalate at the EP-M1 go/no-go if the real sccache
  0.14.0 binary cannot be driven by the fake toolchain. The fallback is in
  `Decision log` D9.
- Performance: stop and escalate if, on this host, one store save of 10,000
  records takes more than 50 ms, or one tick with a reading in flight takes
  more than 20 ms longer than a tick without one (measured in EP-M5).
- Ambiguity: stop and present options if the sccache facts settled in EP-M1
  contradict an axiom in `Verification plan`. A2 and A3 are the critical ones.

## Risks

- R1. The installed binary might not behave as its 0.14.0 source reads on
  the ordering of statistics and reply (axiom A2). A partial footprint, such as
  a miss whose `compilations` has moved but whose `cache_misses` has not, would
  read as a `not_cached` label. That breaks soundness, not just coverage.
  Severity: high. Likelihood: low, because the source shows one guard before
  the reply (S1). Mitigation: EP-M1 reads the counters immediately after each
  of 200 client exits on the real binary. Any partial footprint activates D12
  before any label is emitted.
- R2. Coverage may be very low on a busy host, because a quiet point needs
  `cached_moving == 0`. Severity: medium. Likelihood: medium. Mitigation: H1
  measures coverage, and the report states the bias of quiet windows. This is a
  G1 decision, not an implementation fault.
- R3. Readings cost process spawns. Severity: low. Likelihood: medium.
  Mitigation: a reading is taken only when the open window holds at least one
  closed cached launch, or no baseline exists, and at most once per second (D8).
- R4. A store save blocks the loop. Severity: medium. Likelihood: low.
  Mitigation: bounded set, save at most once a minute and at stop, measured
  against the performance tolerance. If the tolerance is breached, move the
  save to a writer thread after escalation.
- R5. The real-binary check may not accept a fake toolchain, because sccache
  runs `rustc -vV` and `--print=sysroot` and hashes sysroot libraries.
  Severity: medium. Likelihood: medium. Mitigation: EP-M1 is a prototyping
  milestone with an explicit fallback (D9).
- R6. Abandoned `sccache` or `systemctl` children could accumulate as
  zombies. Severity: low. Likelihood: low. Mitigation: a bounded reaper list,
  `SIGKILL` on the deadline, a non-blocking `poll()` each tick, and no new
  query while the reaper list is full (`read_error`).
- R7. Metric readers that predate the release drop `cache_outcome` records
  whole (RFC open question 6). Severity: low. Likelihood: certain for old
  readers. Mitigation: off by default. The users' guide says to enable it only
  after every journal reader is upgraded.

## Progress

- [x] (2026-10-23 19:05Z) Branch renamed to `cache-hit-prediction`. RFC 0002
  read in full.
- [x] (2026-10-23 19:20Z) Wyvern reconnaissance of admission, the supervisor
  loop, `ServerWatch`, `ManagedSccache`, metrics, tracing, status, test
  infrastructure and documentation conventions.
- [x] (2026-10-23 19:25Z) Firecrawl checks of sccache 0.14.0 `ServerStats`,
  `ServerInfo`, `PerLanguageCount`, `CACHE_VERSION` (`b"6"`) and systemd
  `InvocationID` (D-Bus type `ay`).
- [x] (2026-10-23 19:50Z) Axiom A2 settled from the 0.14.0 source (S1). The
  silent footprint and `empty_stats` rule were added (D13).
- [ ] Draft plan reviewed by the expert panel. Revisions applied.
- [ ] Plan approved by the owner. Implementation must not begin before this.
- [ ] EP-M0: ADR 004, fixed hypothesis thresholds, roadmap phase 3.
- [ ] EP-M1: real-binary feasibility and axiom check (prototype; go or
  no-go).
- [ ] EP-M2: pure statistics parsing, footprints and window attribution.
- [ ] EP-M3: pure keys, origins, records and the bounded record set.
- [ ] EP-M4: ports and their adapters.
- [ ] EP-M5: supervisor wiring, metrics, spans and the report section.
- [ ] EP-M6: documentation.
- [ ] G1 (owner): release, enable on the host, collect at least 2,000
  attributed outcomes, record H1 and H3 verdicts and the G1 decision.
- [ ] EP-M7: W2 offline replay and the H2, H4 and H5 verdicts (gated on G1).

## Surprises & discoveries

- S1. Observation: a structured extraction of sccache 0.14.0 `server.rs`
  first hinted that `tx.send` of the compile response might precede some
  statistics updates. A research pass that quoted the source settled the
  question, and A2 holds. In `start_compile_task` (around line 1376) every
  result counter for a request is updated under one `me.stats.lock().await`
  guard, inside the spawned task, before `Ok(res)` is returned. The
  `CompileFinished` body is built only from that resolved future (around lines
  1022-1028). The cache-write counters are awaited later, but they are also in
  place before the reply. Three nuances matter:
  - `compile_requests` (in `call`, around line 864), the classification
    counter and the result counters are separate lock acquisitions. A
    concurrent reading can therefore see a request mid-flight. The quiet
    point excludes this for buildlimits' own requests, and the count rule
    rejects a stray one.
  - Two paths count `requests_executed` and then move no result counter.
    They are `Err(HttpClientError)` (which calls `drop(stats)`) and a panic
    in the spawned task. The truth table models them as the _silent_
    footprint.
  - With no server, `--show-stats --stats-format json` exits 0 and prints a
    full `ServerInfo` with zeroed statistics and the _client's_ version. A
    failure to query prints `sccache: error: ...` and exits 2.

  Evidence: quoted `server.rs`, `commands.rs` and `compiler/rust.rs` at
  `v0.14.0`. The quoting pass also confirmed that `-vV` is `CannotCache`
  ("missing input"), not `NotCompilation`.

  Impact: R1 is downgraded and D12 becomes a contingency that applies only if
  EP-M1 contradicts the source. The `empty_stats` rule is defined in D13, and
  the truth table gains the silent footprint.
- S2. Observation: RFC 0002 says `InvocationID` can be read "as one more
  property name on the `manager.show` call the supervisor already makes to
  inspect the unit". The only `manager.show` callers are
  `ManagedSccache.accounted_drain` and `active_compiles`
  (`buildlimits/sccache.py` lines 221 and 253), and they run only while a group
  is in `SERVER_DRAIN`. That is exactly when a quiet point cannot occur. The
  reachability check is an `AF_UNIX` connect (`sccache.py` lines 185-219), not
  a manager query. Adding `InvocationID` to the drain call would also turn a
  fallback refusal into a drain fault (`bus_properties.service_properties`
  rejects unknown properties, line 177). Impact: the generation needs its own
  bounded, non-blocking query (D2).
- S3. Observation: `UserManager.show` and every `systemd.run` call are
  blocking `subprocess.run` calls with a three-second bound
  (`systemd._transport_bound`). Impact: the generation read cannot reuse them
  inside the loop without breaking C-ISOLATE, so it uses the same non-blocking
  child helper as the statistics read (D2).
- S4. Observation: `metrics_records._source_accepts_event` accepts a
  supervisor event only from `buildlimits-supervisor.service`, and
  `tests/test_metrics_report.py::test_event_source_matrix` is parametrized over
  every event. Impact: both new events are supervisor-owned. The matrix test
  needs their rows.

## Decision log

Decisions D1 to D12 are proposed in this draft and take effect only when the
owner approves the plan. Each one that departs from the letter of RFC 0002 is
marked as a deviation and repeated in ADR 004. The owner approves them by
approving the plan. If a deviation is rejected, the plan returns to `DRAFT`.

- D1. Scope. Decision: W1 (EP-M0 to EP-M6) and W2 (EP-M7). EP-M7 is gated
  on the owner's G1 decision. Rationale: the RFC's acceptance approves W1 now
  and W2 at G1. W3 to W6 each need their own plan once their gates release
  them. Date/Author: 2026-10-23, planning agent.
- D2. Deviation from RFC 0002 §1. Decision: read the server generation with
  its own non-blocking
  `systemctl --user show <sccache_unit> --property=InvocationID --value`,
  through the unit-properties port, on either side of each statistics query. An
  empty value means _no server_. If `systemctl` fails, the read is
  `unavailable`, and the public-bus fallback is not used. Rationale: S2 and S3.
  The RFC explicitly permits treating the fallback's refusal as an unavailable
  reading. Date/Author: 2026-10-23, planning agent.
- D3. Refinement of RFC 0002 §1. Decision: a reading during whose statistics
  query any cached group launched is discarded like an abandoned query. It is
  not a window end and not a baseline, and it reports `outcome_reason`
  `no_quiet_point`. Rationale: the loop keeps admitting during the query
  (C-ISOLATE), so a launch can straddle the reading. Discarding the reading
  keeps every launch in exactly one window, which the RFC's no-double-count
  argument needs. Date/Author: 2026-10-23, planning agent.
- D4. Deviation (vocabulary extension). Decision: `cache_result` gains
  `refused` (the server moved `requests_not_cacheable`), `not_compile`
  (`requests_not_compile`) and `unsupported` (`requests_unsupported_compiler`).
  Rationale: every Cargo build begins with a `-vV` probe that the adapter
  submits as a cacheable request. At 0.14.0 the server refuses it (C4), so solo
  windows that hold only a refusal are common. They must be named for the count
  rule, and H3 tests `requests_not_cacheable` per request, which needs the
  per-request label. This keeps the RFC's distinction that `not_cacheable` is
  only ever a result for a compile that ran. Date/Author: 2026-10-23, planning
  agent.
- D5. Deviation (vocabulary extension). Decision: `outcome_reason` gains
  `unsupported_version`. A statistics document whose `version` is not in the
  supported set (`{"0.14.0"}`) is rejected without attribution. Rationale: the
  RFC requires W1's host check to be repeated after an sccache upgrade before
  recording resumes. The server reports its own version in every statistics
  document (`ServerInfo.version`), so the guard costs nothing. Date/Author:
  2026-10-23, planning agent.
- D6. Refinement (event shape). Decision: two supervisor events.
  `cache_outcome` has two grains, distinguished by the boolean `window_closed`.
  With `window_closed=1` there is one event per cached group whose window
  closed, carrying `cache_result`, `attribution`, `origin`, `outcome_reason` and
  `runtime_seconds`. With `window_closed=0` there is one event per reading
  that closed no window, carrying `cache_result=unattributed`,
  `attribution=none` and its `outcome_reason`, with no `origin` or `runtime`.
  `outcome_store` carries `store_reason`, `store_records` and
  `store_discarded`, and is emitted on load, save, aging and salt failure.
  Rationale: coverage must be computable from the journal alone (group grain).
  Read failures that close no window must still be visible (RFC I3). The RFC's
  `store_reason` needs an emitter that is not a reading. Both events enter
  `EVENTS` in one release (C-SCHEMA). Date/Author: 2026-10-23, planning agent.
- D7. Refinement (privacy). Decision: the argument digest is
  HMAC-SHA-256 under the per-host salt, not a plain SHA-256. Rationale: the
  digest covers `CARGO_MANIFEST_DIR` and absolute source paths, so an unsalted
  digest could be checked against a guessed path elsewhere. That is the exact
  attack the RFC's salt exists to stop. Date/Author: 2026-10-23, planning agent.
- D8. Decision: a reading starts only when every quiet-point clause holds and
  either no baseline exists or the open window holds at least one closed cached
  launch. It starts at most once per `OUTCOME_READ_SECONDS = 1.0`. The deadline
  is `OUTCOME_QUERY_SECONDS = 2.0` for each of the three child queries
  (generation before, statistics, generation after). Rationale: the RFC's "at
  most once per tick" would mean every 100 ms (`TICK_SECONDS = 0.1`). An empty
  window carries no information. Date/Author: 2026-10-23, planning agent.
- D9. Deviation (host-check method). Decision: the RFC's "builds one small
  registry crate cold and then warm" becomes an opt-in test that drives the
  real sccache 0.14.0 binary, on a private socket and a private `SCCACHE_DIR`,
  with a fake Rust toolchain (`tests/toolchain_trees.py`) whose `rustc` answers
  `-vV`, `--print=sysroot` and dep-info and writes plausible outputs. Fallback
  if EP-M1's no-go fires: a documented operator procedure that runs a real
  cold-then-warm build through the live, admitted supervisor after release,
  with recording on. Rationale: compiling a real crate under a private sccache
  server would run `rustc` outside compile admission (C-HOST). The fake
  toolchain exercises the same server code paths (C2, C4, C5) with no compiler.
  Date/Author: 2026-10-23, planning agent.
- D10. Deviation from RFC 0002 §2 (toolchain identity). Decision: the
  toolchain identity is
  `(salted digest of the resolved compiler path,
  version fingerprint, RUSTUP_TOOLCHAIN value or "", sccache CACHE_VERSION)`.
  The version fingerprint is the `librustc_driver-<hash>.so` file name when
  `toolchain_env`'s filesystem recognition finds a real toolchain, and otherwise
  `(st_dev, st_ino, st_size, st_mtime_ns)` of the resolved executable. The
  supervisor does not run `<compiler> -vV`. Rationale: running a
  request-supplied compiler in the supervisor's own control group, outside a
  scope and a token, is a new trust boundary and a blocking process per
  toolchain. The RFC's five-port budget has no port for it. The driver library
  hash changes with every rustc build, and Cargo's `-C metadata` already folds
  the `-vV` text into the argument digest. The residual (a rustup proxy whose
  named channel moved) is a confounder that H5 measures, as the RFC already
  accepts for in-place sysroot changes. Date/Author: 2026-10-23, planning agent.
- D11. Decision: the H1 to H5 thresholds are fixed, as written in RFC 0002
  ("Hypotheses and decision gates"), in ADR 004 in EP-M0, before any data is
  read. Together with the 2,000-outcome minimum, they are constants in the W2
  replay module, and a test pins them to the ADR. Rationale: the RFC's
  acceptance requires thresholds fixed before data. Date/Author: 2026-10-23,
  planning agent.
- D12. Contingency, conditional on A2. Decision: this decision activates only
  if EP-M1 shows that statistics can trail the reply. In that case, a label
  from window _k_ is provisional and is confirmed only when the next accepted
  reading, in the same generation, shows a window _k+1_ whose delta is exactly
  explained by its own launches. A provisional label that cannot be confirmed
  is emitted as `unattributed` with `outcome_reason` `count_mismatch`.
  Rationale: a trailing increment shows up as an unexplained column in the next
  window. Requiring the next window to balance turns a soundness risk into a
  coverage cost. Date/Author: 2026-10-23, planning agent.
- D13. Decision: classify each reading in this order. A query that exits
  non-zero, or cannot be launched, is `read_error`. Text that is not one JSON
  object with a `stats` object is `parse_error`. A `version` outside the
  supported set is `unsupported_version`. Readings in different generations are
  `generation_changed`. An after-reading whose counters are all zero while the
  window holds at least one launch is `empty_stats`. Any counter below the
  baseline is `counter_decreased`. A window holding an unclean group is
  `ineligible_window`. Any of these count failures is `count_mismatch`:
  - Δ`compile_requests` is not the number of launches;
  - the four classification counters do not sum to Δ`compile_requests`;
  - the executed requests are not explained by footprints.

  An all-zero _baseline_ in a running generation is accepted, because a fresh
  server is all zero. Rationale: S1 shows that a client which cannot connect
  prints a zeroed document with exit 0 and its own version. The generation
  check comes first because only it can tell a restart from a continuing server
  (RFC C5). Naming `empty_stats` separately keeps a socket fault from hiding as
  `counter_decreased`. Date/Author: 2026-10-23, planning agent.
- D14. Deviation (vocabulary extension). Decision: `outcome_reason` gains
  `mixed_window`. It is the last rule in D13's order: a window that is eligible
  and balanced, but whose delta is neither one solo footprint nor _n_ copies of
  one unanimous footprint, is `unattributed` with `mixed_window`. Rationale:
  RFC 0002 says `outcome_reason` is `none` exactly when a label was produced,
  and none of its nine tokens describes a sound reading that the counters
  cannot split. Without the token, every busy-period window would be reported
  under a misleading reason, and H1's coverage analysis needs to separate
  "ambiguous" from "broken". Date/Author: 2026-10-23, planning agent.

## Outcomes & retrospective

Nothing yet. This section is filled in at each milestone and at G1.

## Context and orientation

buildlimits is a Python 3.14 package (`buildlimits/`) that rations Rust
compiles on a shared host. The terms below are used throughout this plan.

- Cargo runs `build-limits-rustc` (the _adapter_, `buildlimits/adapter.py`)
  as `RUSTC_WRAPPER`. The adapter sends a _compile request_ (`argv`, `cwd`,
  `env`, `label`, `cacheable`) to the _supervisor_ over a Unix socket.
- The supervisor (`buildlimits/supervisor.py`, class `Supervisor`) is an
  imperative shell around a pure _admission core_ (`buildlimits/admission.py`,
  class `Admission`). Admission performs no input or output and returns
  _effects_ (`buildlimits/effects.py`), which the shell carries out through
  `buildlimits/effect_dispatch.py`.
- A _token_ is a byte read from the shared pool device `/dev/guild`. Each
  admitted request becomes a _group_ (`admission_state.Group`, identifier
  `gN`), run in a systemd transient _scope_. The token returns when the scope
  is empty.
- A group is _cached_ when admission composed it with the _managed sccache
  server_ (`buildlimits-sccache.service`, socket
  `$XDG_RUNTIME_DIR/buildlimits/sccache.sock`). `Groups.launch_spec`
  (`buildlimits/groups.py` line 283) prefixes the sccache client and sets
  `SCCACHE_SERVER_UDS`. The client's output goes straight to the caller, so the
  supervisor never sees whether the compile was a hit.
- `Counts` (`admission_state.py` line 25) is an identifier-free snapshot.
  `cached_moving` counts cached groups that are running or draining, and
  `server_drain` counts groups retained in `SERVER_DRAIN`. A cached group that
  ends abnormally keeps its token in `SERVER_DRAIN` until
  `ManagedSccache.accounted_drain` proves the server stopped and empty.
- `Group.may_orphan_server_work` (`admission_state.py` line 88) is true when
  a group was cancelled, has no recorded exit, or was signalled. Its negation
  is the RFC's "ended with a known exit, uncancelled and unsignalled".
- The loop: `Supervisor.serve` runs `DrainLoop.run` with
  `TICK_SECONDS = 0.1`. Each cycle selects on registered descriptors and then
  calls `Supervisor._tick` (line 326), which calls
  `supervisor_polling.poll_services`. Poll cadences live in
  `supervisor_polling.py`: `SERVER_POLL_SECONDS = 1.0`,
  `HEALTH_POLL_SECONDS = 2.0` and `CACHE_POLL_SECONDS = 5.0`. Any exception
  escaping a cycle counts as an internal fault and degrades health after 20
  faults, so the new code must contain its own failures. `SIGCHLD` wakes the
  selector through `signal.set_wakeup_fd`, and group reaping is per process ID
  (`waitpid(pid, WNOHANG)`), so a new child process of the supervisor is never
  reaped by group code.
- `ServerWatch` (`buildlimits/server_watch.py`) owns reachability
  (`admission.cached`, refreshed every 5 s), the recovery _hold_
  (`cache_allowed()` is false during a hold) and drain release.
  `CacheRecovery.describe(now)["phase"] == "idle"` is the only "recovery idle"
  predicate today (`cache_recovery.py` line 149).
- Metrics: `metrics_schema.py` holds closed tables (`EVENTS`, `VOCABULARIES`,
  `DURATIONS`, `COUNTERS`, `BOOLEANS`). `metrics.Metrics.emit` validates each
  field at the output boundary and drops what fails, isolating sink errors.
  `metrics_report.report` builds sections (`_cache`, near line 160, is the
  model), and `metrics_cli._render` prints every top-level section.
- Tracing: `tracing.JournalTracer.start(operation)` refuses an operation not
  in `tracing_export.OPERATIONS`. `_bounded_attributes` keeps only `outcome`,
  `failure_category`, `stop_mode` and `retry_attempt`, from the
  `tracing_export` vocabularies (ADR 003).
- Settings: `config.SupervisorConfig` is a frozen dataclass. An unknown key
  stops start-up. Enumerations are `str` fields checked by `_RANGE_RULES`.
- Tests: all in `tests/`. Hypothesis state machines (`tests/test_admission.py`
  `AdmissionMachine`), CrossHair contracts (`tests/crosshair_contracts.py`, run
  by `tests/test_crosshair.py`), Syrupy snapshots (`tests/__snapshots__/`),
  pytest-bdd features (`tests/features/`), and opt-in host acceptance marked
  `systemd_user`. `make test-systemd-user` sets
  `BUILD_LIMITS_HOST_ACCEPTANCE=1`. The tests' sccache stand-in is
  `tests/fixtures/fake_sccache.py` (`server` and `client` subcommands only).

The sccache facts this plan depends on (RFC 0002 C1 to C7) were read at tag
`v0.14.0`:

- `--show-stats --stats-format json` prints one `ServerInfo` JSON object. It
  holds `stats` (`ServerStats`), `cache_location`, `cache_size`,
  `max_cache_size`, `use_preprocessor_cache_mode`, `version` and `basedirs`.
- `cache_hits`, `cache_misses` and `cache_errors` are each `PerLanguageCount`
  objects (`{"counts": {...}, "adv_counts": {...}}`). Their total is the sum of
  `counts`.
- The plain counters are `compile_requests`, `requests_unsupported_compiler`,
  `requests_not_compile`, `requests_not_cacheable`, `requests_executed`,
  `non_cacheable_compilations`, `compilations` and `compile_fails`. Durations
  serialize as `{"secs": n, "nanos": n}`.
- The Rust `CACHE_VERSION` is `b"6"`. Rust compiler detection runs
  `rustc -vV` and `rustc --print=sysroot`.

## Conformance basis

- Terms of Reference: none exists for this repository. Upstream intent comes
  from RFC 0002.
- Technical design: RFC 0002, `docs/rfcs/0002-cache-hit-prediction.md`,
  accepted 2026-10-23 at commit `42f40e4`. There is no separate `*-design.md`.
  The developer's guide is the living interface reference.
- ADRs: ADR 003 (bounded tracing) governs the spans. ADR 011 in
  dev-env-rocky (sccache under compile admission) governs token coverage. This
  plan creates ADR 004 (`docs/adr-004-cache-outcome-recording.md`) to record D2
  to D12 and the fixed thresholds.
- Standards: `AGENTS.md`, `docs/developers-guide.md`,
  `docs/documentation-style-guide.md`, `docs/scripting-standards.md`.

Traced requirements use these identifiers:

- RFC-GOAL-1 (record outcomes soundly), RFC-GOAL-3 (decide with evidence).
- RFC-I1 to RFC-I4 (layer 1 safety invariants).
- RFC-PORTS (the five ports and Table 2).
- RFC-OBS (event, reasons, spans, metrics report).
- RFC-STORE (store path, bounds, aging, atomicity, one writer).
- RFC-PRIV (salt, modes, no export).
- RFC-KEY (§2 key derivation, as used by layer 1 records).
- RFC-H1 to RFC-H5 (hypotheses), RFC-W1, RFC-W2 (workstreams),
  RFC-COMPAT (off-by-default key, schema release first).

Trace chains:

```plaintext
RFC-GOAL-1 -> RFC-I1..I4 -> EP-M2 cache_outcome.attribute
  -> tests/test_cache_outcome_attribution.py::test_truth_table, tests/test_cache_outcome_machine.py::OutcomeMachine
RFC-I1 -> C-OBS -> EP-M5 -> tests/test_admission.py::test_import_boundary (extended), tests/test_outcome_noninterference.py::test_admission_effects_identical
RFC-I2 -> C-NOSTART -> EP-M4/EP-M5 -> tests/test_sccache_statistics.py::test_argv_is_show_stats_only, tests/test_outcome_gate.py::test_quiet_point_clauses
RFC-I3 -> C-ISOLATE -> EP-M5 -> tests/test_outcome_loop.py::test_loop_admits_while_query_stalls, tests/test_outcome_reasons.py::test_every_outcome_reason_is_provoked
RFC-PORTS -> EP-M4 -> tests/test_outcome_ports.py::test_every_port_failure_has_its_reason
RFC-STORE, RFC-PRIV -> EP-M3/EP-M4 -> tests/test_outcome_records.py, tests/test_outcome_store.py, tests/test_outcome_key_properties.py
RFC-KEY -> EP-M3 -> tests/test_outcome_key.py
RFC-OBS -> EP-M5 -> tests/test_outcome_metrics.py, tests/__snapshots__/test_metrics_report.ambr
RFC-W1 host check (C2, C4, C5) -> EP-M1 -> tests/test_sccache_real_binary.py (opt-in)
RFC-H1, RFC-H3 -> G1 -> build-limits-metrics cache_outcomes section + build-limits-cache-replay h3
RFC-W2, RFC-H2, RFC-H4, RFC-H5 -> EP-M7 -> tests/test_outcome_replay.py, tests/__snapshots__/test_outcome_replay.ambr
RFC-COMPAT -> C-DEFAULT, C-SCHEMA -> EP-M5
  -> tests/test_outcome_config.py, tests/test_metrics_records_compatibility.py (extended)
```

## Verification plan

### Axioms

Each axiom is taken as given and named so that a later discovery can falsify it
explicitly.

- A1. At sccache 0.14.0, each `CompileResult` moves the counters as in RFC
  0002's footprint table. The source is RFC 0002 §1, from `server.rs`. EP-M1
  confirms hit, miss and refusal on the real binary.
- A2. The server has finished every statistics update for a request before it
  sends that request's `CompileFinished` reply. A reading taken after the
  client has exited therefore sees the complete footprint. Source: S1 (one
  guard in `start_compile_task`, before the response body resolves). EP-M1
  confirms it on the real binary by reading immediately after each client
  exits. If A2 is false there, D12 activates.
- A3. Every `Compile` request increments `compile_requests` exactly once, and
  then exactly one of `requests_unsupported_compiler`, `requests_not_compile`,
  `requests_not_cacheable` or `requests_executed`. An executed request then
  moves at most one result footprint. It moves none for `HttpClientError` or a
  panic (the silent footprint). The one known exception is a panic in
  `compiler_info`'s `expect`, which counts `compile_requests` alone. The count
  rule rejects that window, so it costs coverage, not soundness. Source: S1.
  EP-M1 confirms the refusal path with a `-vV` request.
- A4. systemd assigns a fresh `InvocationID` each time the unit's service
  starts, and the property is empty while no invocation runs. Source:
  `org.freedesktop.systemd1(5)`. The systemd scenario in EP-M5 exercises it.
- A5. `sccache --show-stats` uses `connect_to_server` and never starts a
  server (RFC C5).
- A6. A scope reported empty means the sccache client process has exited.
  With A2, its request's counters are final. Source: the existing `group_drain`
  emptiness contract.
- A7. Cargo sets `CARGO_PKG_NAME`, `CARGO_PKG_VERSION` and
  `CARGO_MANIFEST_DIR` for each compiler invocation (Cargo Book).
- A8. `os.replace` within one directory is atomic on Linux local
  filesystems.

### Obligations

Each obligation below names its method, domain, artefact, evidence and
non-vacuity argument.

O1. Footprint decoding is sound. A solo window's label is the true outcome of
its single request, and a unanimous window's label is the true outcome of every
one of its requests. Every window whose delta is a sum of two or more distinct
footprints is `unattributed`.

- Method: exhaustive parameterized enumeration plus a CrossHair contract.
- Rationale: the footprint table is finite, so every multiset of footprints
  can be enumerated up to a bound. CrossHair covers the decoding function over
  all non-negative integer deltas.
- Domain: every multiset of size 1 to 4 drawn from the twelve footprints:
  - hit, miss, and miss with read error;
  - not_cached, not_cacheable and failed;
  - error-fails and error-cache;
  - silent (executed, no result);
  - refused, not_compile and unsupported.

  The ambiguous pair the RFC names is a required row. A solo silent request
  must be `unattributed` with `count_mismatch`, never a label.
- Artefacts: `tests/test_cache_outcome_attribution.py::test_truth_table` and
  `::test_every_mixed_multiset_is_unattributed`, and
  `tests/crosshair_contracts.py::decoding_never_labels_mixed`.
- Evidence: `uv run pytest tests/test_cache_outcome_attribution.py`, which
  fails red with `ModuleNotFoundError` before EP-M2.
- Non-vacuity: the enumeration asserts that it produced at least one solo
  window of each class, at least one unanimous window for each unanimous class,
  and at least 100 mixed multisets. Three mutations must each fail a test:
  deriving hits from `compilations`, dropping the `cache_errors` footprint, and
  allowing a unanimous `error`.

O2. Every cached launch falls in exactly one closed window. Every cached group
that launched while recording was on yields exactly one group-grain
`cache_outcome` event: when its window closes, or as `unattributed`
`no_quiet_point` when the supervisor stops with the window open.

- Method: a Hypothesis `RuleBasedStateMachine`.
- Domain: interleavings of submit, token, launch (cached or direct), launch
  failure, clean exit, signalled exit, cancel, `SERVER_DRAIN` and release,
  reading start, reading completion (prompt, slow or abandoned), server restart
  and stop.
- Artefact: `tests/test_cache_outcome_machine.py::OutcomeMachine`.
- Evidence: an invariant compares the set of event group IDs with the set of
  cached launches at teardown.
- Non-vacuity: `hypothesis.event` counts must show solo windows, unanimous
  windows, rejected windows, an abandoned query, a straddled query (D3) and a
  restart. A teardown assertion fails the run if any class is missing.
  Mutation: treat a straddled reading as a baseline. That double-counts or
  mislabels, and the machine must find it.

O3. Attribution is sound against a model server. Every attributed label equals
the model's true outcome. This holds under overlapping cached and direct
groups, abnormal endings, restarts (including one that places the new
generation's counters above the old) and trailing updates when D12 is active.

- Method: the same state machine, with a model sccache server that holds
  per-request truth and produces statistics documents.
- Non-vacuity: at least one solo and one unanimous label must be emitted per
  run. Mutation: remove the generation check. The restart-above-old-values rule
  must then produce a wrong label.

O4. Non-interference (I1). For any sequence of admission events, the effects
`Admission` returns are identical with recording on and off.

- Method: a differential state machine that drives two supervisors' admission
  cores and shells with fake ports, plus the AST import boundary.
- Artefacts: `tests/test_outcome_noninterference.py`, and the extended
  `tests/test_admission.py` import table.
- Non-vacuity: the run must reach a cached launch, a `SERVER_DRAIN`
  retention and a completed reading. Mutation: let the shell delay
  `token_arrived` while a query is outstanding. The differential must fail.

O5. Loop liveness under a stalled query (I3). While a statistics query is
outstanding for its full deadline, the loop accepts a request, admits it and
returns a token. The stalled child is killed and reaped.

- Method: an example test with a scripted adapter that sleeps, and a real
  `DrainLoop` with an injected clock.
- Artefact: `tests/test_outcome_loop.py::test_loop_admits_while_query_stalls`.
- Evidence: it counts loop iterations during the stall (at least 10 at
  `TICK_SECONDS`), asserts one admission and one token return, and asserts that
  `os.waitpid` reports the child reaped.
- Non-vacuity: replacing the non-blocking adapter with a blocking
  `subprocess.run` makes the iteration count 0, and the test fails.

O6. Reason vocabulary completeness. Each `outcome_reason` (`none`, `timeout`,
`read_error`, `parse_error`, `empty_stats`, `generation_changed`,
`counter_decreased`, `count_mismatch`, `ineligible_window`, `no_quiet_point`,
`unsupported_version`, `mixed_window`), each `store_reason` (`corrupt`,
`unreadable`, `version_mismatch`, `tag_dropped`, `salt_unavailable`,
`save_failed`, `expired`, `none`) and each port failure in RFC Table 2 is
provoked by a test that asserts the reason.

- Method: parameterized tests over fake ports.
- Artefacts: `tests/test_outcome_reasons.py` and `tests/test_outcome_ports.py`.
- Non-vacuity: a meta-test compares the parametrized IDs with the schema
  vocabulary, so a new token without a test fails. A timed-out reading asserts
  that no label exists, never that a label is unchanged (the RFC's vacuity
  warning).

O7. Privacy. No stored key, record or journal field contains a path separator
or any component of a local path. With the salt unavailable, no record is
written and the attribution event is unchanged.

- Method: Hypothesis over generated manifest directories, Cargo homes and
  argument vectors, plus an example test for the salt.
- Artefact: `tests/test_outcome_key_properties.py`.
- Non-vacuity: the generator must produce registry, git and path origins
  (classified with `hypothesis.event`). Mutation: store the raw path for
  `path:` origins. The property must fail.

O8. Store bounds and atomicity. The set never exceeds 10,000 records. A record
untouched for 30 days is expired on the next save. A save interrupted before
`os.replace` leaves the previous file readable. A file with another format
version reports `version_mismatch` and is not read further.

- Method: Hypothesis over add, age, save and load sequences, plus
  fault-injected saves.
- Artefacts: `tests/test_outcome_records.py` and `tests/test_outcome_store.py`.
- Non-vacuity: the generator must reach the bound and an expiry.

O9. The generation baseline moves across restarts. After a restart, the first
window in the new invocation is attributed against the new baseline. The
reading that exposed the restart becomes the baseline. It is never attributed.

- Method: a unit test, plus a `systemd_user` scenario with the extended fake
  sccache.
- Artefact: `tests/test_cache_outcomes_systemd.py`.

O10. Key normalization. Registry, git and path origins, a non-default
`CARGO_HOME`, missing variables, pre-release versions with hyphens, and
`--crate-name=foo` as well as `--crate-name foo`.

- Method: parameterized tests.
- Artefact: `tests/test_outcome_key.py`.

Residual gaps: the attribution's soundness on the real server rests on A1 to
A3. EP-M1 checks a handful of windows against the real binary, not every
interleaving. The model-server state machine covers the interleavings under the
axioms. The RFC's own caveat stands: labels from solo and unanimous windows
favour quiet, uniform periods.

## Plan of work

The design follows the repository's functional core and imperative shell
(developer's guide §3) and the hexagonal rule that the domain owns its ports.
Every module below is a new file under `buildlimits/` unless stated otherwise.

Domain (pure: no input or output, no clock, no `os`, no `subprocess`):

- `cache_stats.py`: `StatsReading`, a frozen dataclass of the counters, with
  totals for the `PerLanguageCount` fields. Also
  `parse_statistics(text: str,
  supported: frozenset[str]) -> StatsReading | ReadFailure`,
  which yields `parse_error`, `empty_stats` or `unsupported_version`.
- `cache_outcome.py`: the RFC's
  `attribute(before, after, window) -> Attribution`. It holds the footprint
  table (`FOOTPRINTS`, as data), the count rule, the solo and unanimous
  decoding, and the generation and decrease checks. It returns per-group labels
  or a reason. It holds no state.
- `outcome_window.py`: `OutcomeWindow`, the pure ledger of cached launches
  since the baseline. `launched(group_id, key_material, at)` records a launch,
  `ended(group_id, clean, runtime)` records an ending, `dropped(group_id)`
  handles a launch failure, `quiet()` reports whether every entry has ended, and
  `cut() -> tuple[WindowEntry, ...]` closes the window. It keeps the launch
  sequence counter that D3 compares.
- `outcome_key.py`: `crate_name(argv)` (both `--crate-name` forms),
  `origin(env, salt)`, `argument_digest(argv, env, salt)`,
  `toolchain_identity(...)` (from a fingerprint the adapter supplies) and
  `cacheability_features(argv)` (the C4 predicate H3 tests). It takes the salt
  as a value.
- `outcome_records.py`: `OutcomeRecord`, `OutcomeSet` (bounded, aging,
  tag-dropping, with a format version) and `encode(set)` and
  `decode(text) -> OutcomeSet | StoreFailure`.
- `outcome_ports.py`: `typing.Protocol` ports and result types for
  `OutcomeClock`, `StatisticsPort`, `UnitPropertiesPort`, `OutcomeStorePort` and
  `SaltPort`, with failure alternatives as frozen dataclasses carrying a
  reason token (RFC Table 2). The non-blocking ports use a
  `start(...) -> Ticket` and `poll(ticket, now) -> Pending | Ok | Failed`
  shape, so that no port call ever waits.

Application service:

- `outcome_reading.py`: `OutcomeRecorder`, the reading sequencer. Its states
  are `idle`, `generation_before`, `statistics`, `generation_after` and
  `settle`. On each `step(gate, now)` it advances at most one non-blocking
  poll. It holds the baseline and the window, and returns a `StepResult`
  carrying events to emit, records to add, spans to write and reasons. It never
  emits or saves anything itself (RFC "Ports").

Adapters (shell):

- `outcome_children.py`: `NonBlockingChild`, which starts an argv with
  `subprocess.Popen` and its stdout set to `O_NONBLOCK`. It reads up to 64 KiB
  per poll and kills the child on the deadline. A bounded reaper list (eight
  children) is polled with `Popen.poll()` each tick. This is the only process
  code. The statistics and unit-property adapters share it.
- `sccache_statistics.py`: `SccacheStatistics(StatisticsPort)`, which runs
  `(sccache_executable, "--show-stats", "--stats-format", "json")` with the
  environment `{"SCCACHE_SERVER_UDS": socket, "PATH": ..., "HOME": ...}`. The
  argv is a module constant that a test pins.
- `unit_generation.py`: `UnitGeneration(UnitPropertiesPort)`, which runs
  `("systemctl", "--user", "show", unit, "--property=InvocationID", "--value")`.
- `outcome_store.py`: `FileOutcomeStore(OutcomeStorePort)` and
  `FileSalt(SaltPort)`, under `$XDG_STATE_HOME/buildlimits/` (default
  `~/.local/state/buildlimits/`, mode 0700). It holds `cache-outcomes.json` and
  `cache-outcomes.salt` (mode 0600, 32 random bytes from `secrets.token_bytes`,
  hex). Writes use a temporary file in the same directory, `fsync` and
  `os.replace`.
- `supervisor_outcomes.py`: `OutcomeShell`, the composition point. It builds
  the quiet-point gate from `Admission.counts()`, `ServerWatch.cache_allowed()`,
  `admission.cached`, the recovery phase and the barrier path. It feeds
  `OutcomeWindow` from launch and emptied hooks, steps the recorder each tick,
  emits metrics and spans, saves the store on its timer and at stop, and loads
  it at start. `Supervisor` gains only three calls: build at start, `on_tick`,
  and `close` at stop. The launch and emptied hooks go in
  `supervisor_placement.launch` and in the `_tick` line that calls
  `group_emptied`. They read `Group.may_orphan_server_work` before the call.

Existing modules touched:

- `config.py`: `cache_outcomes: str = "off"`,
  `CACHE_OUTCOMES = ("off", "record")` and one `_RANGE_RULES` row.
- `metrics_schema.py`: the `cache_outcome` and `outcome_store` events, and
  the vocabularies `cache_result`, `attribution`, `origin`, `outcome_reason` and
  `store_reason`. It adds the counters `store_records` and `store_discarded`
  and the boolean `window_closed`.
- `metrics.py`: re-exports, if any are needed.
- `metrics_records.py`: source ownership for the two events.
- `metrics_report.py`: a `cache_outcomes` section (counts by `cache_result`,
  `attributed`, `groups`, `coverage`, reasons and `store_reasons`), absent when
  no event exists, following `metrics_pressure_report`'s optional pattern.
- `tracing_export.py`: `OPERATIONS` gains `statistics_read`,
  `outcome_store_load` and `outcome_store_save`. `FAILURE_CATEGORIES` gains the
  `outcome_reason` and `store_reason` failure tokens, so that a span's
  `failure_category` names the reason (ADR 003 shape).
- `supervisor.py`, `supervisor_placement.py` and `supervisor_polling.py`:
  hooks only.
- `tests/fixtures/fake_sccache.py`: a `--show-stats --stats-format json`
  client mode, and server-side counters with scripted outcomes driven by
  `FAKE_SCCACHE_SCRIPT` (one JSON line per request, such as
  `{"result": "hit"}`). An unscripted request keeps today's behaviour and
  counts as a miss.

W2:

- `outcome_replay.py` (pure) computes the outcome mix, coverage by origin
  (from journal records), H3 agreement, H4 registry hit rates after the first
  observation, and H5 flip rates excluding identity changes. It also runs a
  base-rate predictor against two candidates (last outcome, and a Beta estimate
  over the last eight outcomes with an origin prior), reporting Brier scores.
  H2 needs child sightings that W1 does not collect (see EP-M7). The thresholds
  are module constants pinned to ADR 004.
- `replay_cli.py` provides the `build-limits-cache-replay` console script
  (`pyproject.toml` `[project.scripts]`), with text and `--json` output.

The stages are as follows:

- Stage A (EP-M0, EP-M1): no production code. Record the decisions and the
  thresholds, then prove the real-binary facts or take the fallback.
- Stage B (each of EP-M2 to EP-M5): red tests first. Behaviour-driven
  development (BDD) features, property machines and truth tables fail for the
  expected reason (`ModuleNotFoundError` or a strict `xfail`).
- Stage C: the implementation and its verification artefacts together.
- Stage D (EP-M6): documentation, refactor and the full gates.

## Milestones and plateaus

Each milestone ends with `make check-fmt`, `make typecheck`, `make lint` and
`make test` passing. Run them sequentially, through the `scrutineer` subagent.
EP-M5 additionally runs `make test-systemd-user`. Commit at least once per
milestone, and commit separately for each red, green or refactor step that
leaves the tree green.

### EP-M0: decisions recorded before any data

- Outcome: `docs/adr-004-cache-outcome-recording.md` (Status: Proposed, then
  Accepted on plan approval) records D2 to D12, the fixed H1 to H5 thresholds,
  the 2,000-outcome minimum and the vocabulary. RFC 0002 gains a link to it.
  `docs/roadmap.md` gains phase 3, "Record and evaluate cache outcomes (RFC
  0002)", with steps 3.1 (W1) and 3.2 (W2) and tasks matching EP-M1 to EP-M7.
  `docs/contents.md` indexes ADR 004.
- Requirements: RFC-H1 to RFC-H5 (thresholds fixed), RFC-W1 and RFC-W2
  (roadmap).
- Acceptance: `make markdownlint`, `make nixie` and `make check-fmt` pass.
- Conformance check: no code changed. The thresholds are copied verbatim from
  RFC 0002.
- Recovery: documentation only, so revert the commit.
- Remaining gaps: everything else.
- Compatibility decision: none.

### EP-M1 (prototype): real sccache 0.14.0 behaves as read

- Outcome: `tests/test_sccache_real_binary.py`, marked `sccache_host_check`
  and skipped unless `BUILD_LIMITS_SCCACHE_HOST_CHECK=1` and
  `sccache --version` reads `sccache 0.14.0`. It starts a private server
  (`SCCACHE_SERVER_UDS` in a temporary directory, `SCCACHE_DIR` in a temporary
  directory, `SCCACHE_IDLE_TIMEOUT=0`). It drives the server with a fake
  toolchain built by `tests/toolchain_trees.py::make_toolchain`, whose `rustc`
  is a Python script that answers `-vV`, `--print=sysroot`, `--emit=dep-info`
  and a full emit. It then asserts:
  - a cold request moves the miss footprint, a warm identical request moves
    only `cache_hits` (A1), and the client's output and exit are identical in
    both cases (C2);
  - `fake-rustc -vV` through the client moves `requests_not_cacheable` and
    not `requests_not_compile` (C4, A3);
  - a reading taken immediately after each client exits already shows the
    whole footprint, repeated 200 times (A2). Any partial footprint is
    recorded as evidence that D12 is needed;
  - the statistics document's `version` is `0.14.0` and its `stats` keys are
    a superset of the ones `cache_stats` parses. A redacted copy is saved as
    the fixture `tests/fixtures/sccache_0_14_0_stats.json` for EP-M2.
  - `make test-sccache-host` runs it.
- Go or no-go: go if every assertion passes. If the fake toolchain cannot get
  past sccache's compiler detection within a three-hour budget, take D9's
  fallback. Document the operator procedure in the users' guide in EP-M6. The
  A2 evidence then comes from G1's host data together with D12, which becomes
  mandatory.
- Requirements: RFC-W1 host check, A1 to A3, A5.
- Conformance check: no live service is touched, and no real compiler runs
  (C-HOST).
- Recovery: the test is additive and opt-in, so delete it to discard the
  prototype.
- Remaining gaps: recording itself.
- Compatibility decision: none.

### EP-M2: pure attribution

- Outcome: `cache_stats.py`, `cache_outcome.py` and `outcome_window.py`, with
  O1, O2 (window ledger part) and O3 discharged.
- Red: create `tests/test_cache_outcome_attribution.py`,
  `tests/test_cache_stats.py`, `tests/test_cache_outcome_machine.py` and the
  BDD feature below. Run the command below and expect collection errors
  (`ModuleNotFoundError: buildlimits.cache_outcome`).

  ```bash
  uv run pytest tests/test_cache_outcome_attribution.py tests/test_cache_stats.py \
    tests/test_cache_outcome_machine.py tests/test_cache_outcome_rules_behaviour.py
  ```

- Green: implement until those tests pass. Add the CrossHair contract.
- Feature `tests/features/cache_outcome_rules.feature` (pytest-bdd, steps in
  `tests/test_cache_outcome_rules_behaviour.py`, no systemd):

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
      When a reading shows one hit, one miss and one refusal
      Then every group is "unattributed" with attribution "none"
      And the outcome reason is "mixed_window"

    Scenario: A restart is caught even when counters climb past the old values
      Given a baseline reading in generation "a" with compile_requests 5
      And one cached group that ended cleanly
      When a reading in generation "b" shows compile_requests 9
      Then the group is "unattributed" with outcome reason "generation_changed"
      And the generation "b" reading is the next baseline

    Scenario: A signalled group makes its window ineligible
      Given a baseline reading in generation "a"
      And one cached group that was signalled
      When a reading in generation "a" shows a miss
      Then the group is "unattributed" with outcome reason "ineligible_window"

    Scenario: Cargo's version probe is a refusal, not a non-cacheable compile
      Given a baseline reading in generation "a"
      And one cached group that ended cleanly
      When a reading shows requests_not_cacheable up by 1 and compile_requests up by 1
      Then the group is labelled "refused" with attribution "solo"
  ```

- Requirements: RFC-GOAL-1 and RFC-I4.
- Conformance check: the domain modules import nothing from `os`,
  `subprocess`, `socket` or `buildlimits` shell modules. A new AST test,
  `tests/test_outcome_boundaries.py`, pins this.
- Recovery: the modules are new and unreferenced.
- Remaining gaps: keys, ports and wiring.
- Compatibility decision: none.

### EP-M3: keys and records

- Outcome: `outcome_key.py` and `outcome_records.py`, with O7, O8 (pure part)
  and O10.
- Red: `tests/test_outcome_key.py`, `tests/test_outcome_key_properties.py`
  and `tests/test_outcome_records.py`.
- Green: implement.
- Snapshot: `tests/__snapshots__/test_outcome_records.ambr` pins the encoded
  JSON document shape for two records, with fixed salt and timestamps. It is
  paired with a round-trip semantic assertion.
- Requirements: RFC-KEY, RFC-PRIV and RFC-STORE (pure part).
- Conformance check: the salt is a parameter, and no function reads the
  environment of the supervisor process.
- Remaining gaps: the file store and wiring.
- Compatibility decision: none. The store format is new. Version 1 is
  written, and anything else reports `version_mismatch`.

### EP-M4: ports and adapters

- Outcome: `outcome_ports.py`, `outcome_children.py`,
  `sccache_statistics.py`, `unit_generation.py` and `outcome_store.py`.
- Red: `tests/test_outcome_ports.py` (fakes for every port, with each Table 2
  failure provoked), `tests/test_sccache_statistics.py` (a real child running
  `tests/fixtures/fake_sccache.py client --show-stats`, plus a deliberately
  slow script for the deadline; argv pinned) and `tests/test_outcome_store.py`
  (temporary `XDG_STATE_HOME`, modes 0700 and 0600, unreadable file, refused
  save on a read-only directory, interrupted save, version mismatch).
- Green: implement. Extend `tests/fixtures/fake_sccache.py` with statistics
  and scripts, and extend `tests/test_fake_sccache.py`.
- Requirements: RFC-PORTS, RFC-I2 and RFC-STORE.
- Conformance check: each adapter implements a domain-owned Protocol, which
  `ty` verifies through typed construction sites. The adapters import no
  admission module.
- Remaining gaps: wiring.
- Compatibility decision: none.

### EP-M5: wiring, metrics, spans and report

- Outcome: `outcome_reading.py`, `supervisor_outcomes.py`, the config key,
  the schema entries, the tracing operations, the report section, and hooks in
  `supervisor.py`, `supervisor_placement.py` and `supervisor_polling.py`.
- Red:
  - `tests/test_outcome_config.py` (`off` default, `record`, a rejected
    value);
  - `tests/test_outcome_gate.py` (each quiet-point clause false, in turn,
    blocks a query);
  - `tests/test_outcome_reasons.py` (O6);
  - `tests/test_outcome_loop.py` (O5);
  - `tests/test_outcome_noninterference.py` (O4);
  - `tests/test_outcome_metrics.py` (event fields, `window_closed`, report
    section);
  - updated `tests/test_metrics_report.py::test_event_source_matrix`;
  - an extended `tests/test_metrics_records_compatibility.py`;
  - a `metrics_report` snapshot for the new section;
  - `tests/test_cache_outcomes_systemd.py` with
    `tests/features/cache_outcomes_on_systemd.feature`, shown below.
- Green: implement.
- Feature `tests/features/cache_outcomes_on_systemd.feature`, marked
  `systemd_user` through `systemd_harness.systemd_user_marks`:

  ```gherkin
  Feature: Recording cache outcomes under a real user manager
    Background:
      Given an isolated supervisor with cache_outcomes "record" and a scripted fake sccache

    Scenario: Labels follow the script
      Given the fake sccache scripts "miss" then "hit"
      When two cached compiles run one after the other
      Then the journal has cache_outcome events "miss" then "hit" with attribution "solo"
      And the outcome store holds two records with the same key

    Scenario: No query runs while the restart barrier exists
      Given the cache recovery barrier is present
      When a cached compile finishes
      Then no statistics query was started

    Scenario: The first window after a restart uses the new invocation
      Given one attributed compile in the first invocation
      When the cache unit restarts and its counters climb past the old values
      And one cached compile runs
      Then that compile is attributed against the new invocation

    Scenario: A stalled statistics query does not stall admission
      Given the fake sccache answers statistics after 5 seconds
      When a cached compile finishes and another compile is submitted
      Then the second compile is admitted while the query is outstanding
      And the reading is reported with outcome reason "timeout"
  ```

- Requirements: RFC-I1 to RFC-I3, RFC-OBS, RFC-COMPAT and RFC-STORE (one
  writer: in this plan, layer 1 is the only writer).
- Acceptance: with `cache_outcomes = "record"`, the systemd scenarios pass.
  With `off`, the existing suite passes unchanged and `strace`-free tests show
  no new child (the shell is not constructed).
- Conformance check: O4 passes. `supervisor.py` stays under 400 lines. No
  admission file changed.
- Recovery: the key defaults to `off`, so reverting the hooks restores today's
  behaviour.
- Remaining gaps: documentation, and W2.
- Compatibility decision: the new events are a schema addition that old
  readers drop whole (RFC-COMPAT, R7). They are gated by the off-by-default
  key. No source compatibility layer is needed.

### EP-M6: documentation

- Outcome:
  - `docs/users-guide.md`: Table 2 gains `cache_outcomes`. "Metrics"
    describes the `cache_outcomes` section, the store path and modes, how to
    delete the store, and that enabling must wait until every journal reader
    is upgraded. A new subsection, "Record cache outcomes", covers operation
    and, if D9's fallback was taken, the operator host-check procedure.
  - `docs/developers-guide.md`: §4 module table rows; §6 the outcome shell
    and its non-blocking children; §9 the reading sequencer, the ports and
    their reasons, events and spans; §11.2 new helpers (the fake sccache
    script and the fake ports); §11.3 the new state machines and CrossHair
    contract; §11.4 `make test-sccache-host`. A new convention: _non-blocking
    child queries_ (`outcome_children`) are the only way the loop may run a
    helper process, and future pollers reuse that helper.
  - ADR 004 status Accepted. `docs/repository-layout.md` only if a new
    top-level path appears.
- Acceptance: `make markdownlint`, `make nixie`, `make check-fmt` and
  `make spelling` pass.
- Compatibility decision: none.

### G1 (owner gate, not agent work)

Release, enable `cache_outcomes = "record"` on the host after every journal
reader is upgraded, and run until at least 2,000 attributed outcomes exist.
Then record the H1 verdict (coverage from `build-limits-metrics`, label
agreement from EP-M1 and the tests) and the H3 verdict
(`build-limits-cache-replay h3`, a pure subcommand delivered with EP-M7, or by
hand from the store). Record the G1 decision (`proceed`, `narrow` or `stop`) in
`Decision log`. The plan's status is `BLOCKED` until then.

### EP-M7: W2 offline replay (after G1 says proceed or narrow)

- Outcome: `outcome_replay.py` and `replay_cli.py` provide
  `build-limits-cache-replay [--store PATH] [--since 30d] [--json]`, which
  prints the H3, H4 and H5 verdicts against the ADR 004 thresholds, together
  with the sample sizes and the RFC's caveat on quiet windows. Below 2,000
  attributed records it prints `insufficient`.
- H2: W1 deliberately collects no child sightings, so H2 is reported as
  `not_collected`. If G1 records coverage below 5%, child sighting becomes a
  separately planned task. Raise this at the plan's approval.
- Red: `tests/test_outcome_replay.py` (synthetic record sets with known
  verdicts at, above and below each threshold, plus `insufficient`), a
  Hypothesis property that the Brier score of the base-rate predictor is
  invariant to record order, and `tests/__snapshots__/test_outcome_replay.ambr`
  for text and JSON output, paired with semantic assertions.
- End to end: `tests/test_replay_cli.py` runs the console script on a fixture
  store and asserts the exit status (0 for a report, 1 for an unreadable store,
  2 for bad options).
- Requirements: RFC-W2, RFC-H3 to RFC-H5 and RFC-GOAL-3.
- Compatibility decision: none.

## Concrete steps

Run every command from the worktree root
`/home/leynos/.lody/repos/github---leynos---buildlimits/worktrees/ac9f6ac4-ab16-4230-a4f9-0b19d16fbf25`.

1. Confirm the branch:

   ```bash
   git branch --show-current
   ```

   Expected: `cache-hit-prediction`.

2. For each milestone, write the red tests, then run only those tests and
   capture the output:

   ```bash
   uv run pytest <new test files> 2>&1 | tee /tmp/red-buildlimits-cache-hit-prediction.out
   ```

   Expected: failures for the stated reason only. The usual reasons are
   `ModuleNotFoundError` or `XPASS(strict)` never appearing.

3. Implement, then rerun the same command and expect every test to pass.

4. Run the gates sequentially, through `scrutineer`. To run them by hand:

   ```bash
   make check-fmt 2>&1 | tee /tmp/check-fmt-buildlimits-cache-hit-prediction.out
   make typecheck 2>&1 | tee /tmp/typecheck-buildlimits-cache-hit-prediction.out
   make lint 2>&1 | tee /tmp/lint-buildlimits-cache-hit-prediction.out
   make test 2>&1 | tee /tmp/test-buildlimits-cache-hit-prediction.out
   ```

   EP-M5 adds:

   ```bash
   make test-systemd-user 2>&1 | tee /tmp/test-systemd-user-buildlimits-cache-hit-prediction.out
   ```

   EP-M1 runs, opt-in:

   ```bash
   BUILD_LIMITS_SCCACHE_HOST_CHECK=1 make test-sccache-host 2>&1 | tee /tmp/sccache-host-buildlimits-cache-hit-prediction.out
   ```

5. Commit with an imperative subject of about 50 characters, using the
   attribution trailer from the session instructions.

6. Update `Progress` with a UTC timestamp after every commit.

## Validation and acceptance

The work is done when the following all hold:

- Tests: `make test` passes, and every new test named in `Milestones` exists
  and fails before its implementation, with the red log kept under `/tmp`.
  `make test-systemd-user` passes the four new scenarios. The opt-in host check
  passes on a host with sccache 0.14.0, or the plan records the D9 fallback.
- Verification: O1 to O10 are discharged, each with its non-vacuity
  evidence. The mutation controls listed in each obligation are run once, with
  temporary edits that are never committed, and each fails the named test. The
  output goes to `/tmp/mutation-buildlimits-cache-hit-prediction.out`.
- Lint and typecheck: `make lint` and `make typecheck` pass with no new
  suppression. Any suppression links its planned fix (AGENTS.md).
- Performance: in EP-M5 a test measures one save of 10,000 synthetic records
  on the host. If it takes 50 ms or more, escalate (Tolerances).
- Behaviour: with `cache_outcomes = "record"` on the isolated harness,
  `build-limits-metrics --source supervisor` prints a `cache_outcomes` section
  whose `results` match the fake sccache script.

## Idempotence and recovery

Every step is additive until EP-M5's hooks. Rerunning tests is safe. The store
lives under the test's own `XDG_STATE_HOME`. Systemd scenarios use uniquely
named units that the harness removes. To roll back recording on a host, set
`cache_outcomes = "off"` and restart the supervisor through the documented
graceful restart. Deleting `~/.local/state/buildlimits/` removes every trace,
which costs only the replay.

## Artefacts and notes

The statistics document is abbreviated here to the fields `cache_stats` reads.
The full shape is pinned by EP-M1's fixture.

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

These names and signatures must exist at the end of each milestone. Type names
are final, and private helpers are not listed.

```python
# buildlimits/cache_stats.py (EP-M2)
@dc.dataclass(frozen=True, slots=True)
class StatsReading:
    compile_requests: int
    requests_unsupported_compiler: int
    requests_not_compile: int
    requests_not_cacheable: int
    requests_executed: int
    cache_hits: int
    cache_misses: int
    cache_errors: int
    non_cacheable_compilations: int
    compilations: int
    compile_fails: int
    version: str

def parse_statistics(text: str, *, supported: frozenset[str]) -> StatsReading | ReadFailure: ...

# buildlimits/cache_outcome.py (EP-M2)
type CacheResult = Literal["hit", "miss", "not_cacheable", "not_cached", "failed",
                           "error", "refused", "not_compile", "unsupported", "unattributed"]
type AttributionKind = Literal["solo", "unanimous", "none"]

@dc.dataclass(frozen=True, slots=True)
class Reading:
    generation: str | None      # None means no server
    stats: StatsReading

@dc.dataclass(frozen=True, slots=True)
class WindowEntry:
    group_id: str
    clean: bool                 # not Group.may_orphan_server_work
    runtime_seconds: float

@dc.dataclass(frozen=True, slots=True)
class Attribution:
    labels: tuple[tuple[str, CacheResult], ...]
    kind: AttributionKind
    reason: str                 # an outcome_reason token

def attribute(before: Reading, after: Reading, window: tuple[WindowEntry, ...]) -> Attribution: ...

# buildlimits/outcome_ports.py (EP-M4)
class StatisticsPort(typ.Protocol):
    def start(self, now: float) -> Ticket: ...
    def poll(self, ticket: Ticket, now: float) -> Pending | StatisticsText | PortFailure: ...

class UnitPropertiesPort(typ.Protocol):
    def start(self, now: float) -> Ticket: ...
    def poll(self, ticket: Ticket, now: float) -> Pending | GenerationValue | PortFailure: ...

class OutcomeStorePort(typ.Protocol):
    def load(self) -> OutcomeSet | StoreFailure: ...
    def save(self, records: OutcomeSet) -> Saved | StoreFailure: ...

class SaltPort(typ.Protocol):
    def acquire(self) -> bytes | StoreFailure: ...

class OutcomeClock(typ.Protocol):
    def monotonic(self) -> float: ...
    def wall(self) -> float: ...
```

There are no new dependencies. The plan uses the standard library, and the
existing development tools: pytest, pytest-bdd, Hypothesis, Syrupy and
CrossHair.

Skills and documents to consult while implementing:

- Skills: `execplans` (keep this plan current), `hexagonal-architecture`
  (domain and port boundaries), `python-router` and then
  `python-types-and-apis` (Protocols and `Literal` unions),
  `python-data-shapes` (frozen dataclasses for readings and records),
  `python-concurrency` (the non-blocking child and the reaper),
  `python-errors-and-logging` (failure alternatives instead of exceptions),
  `python-testing`, `hypothesis` and `crosshair` (O1 to O8),
  `python-verification` (choosing the adversary), `en-gb-oxendict-style`
  (documentation), `commit-message`, and `pr-creation`.
- Documents: RFC 0002 (every section), ADR 003, ADR 001 (graceful restart and
  status conventions), `docs/developers-guide.md` §§3 to 6, 9 and 11,
  `docs/users-guide.md` "Supervisor settings" and "Metrics",
  `docs/documentation-style-guide.md` (ADR and roadmap formats), and
  `docs/scripting-standards.md` (for `make test-sccache-host` helpers).

## Revision note

2026-10-23: initial draft from RFC 0002, Wyvern reconnaissance and Firecrawl
checks of sccache 0.14.0 and systemd. Awaiting axiom A2's source result,
expert-panel review and owner approval.
