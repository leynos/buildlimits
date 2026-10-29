# Architectural decision record (ADR) 005: kache as an in-scope cache backend

## Status

Accepted. Admitted compilations may be composed with kache instead of sccache,
selected by `cache_mode = "kache"`. kache runs a cache miss as a child of the
wrapper inside the admitted scope, so kache groups are admitted as direct work
is: a group returns its token when its scope empties, and never enters
`SERVER_DRAIN`, the start-up quiesce or the recovery transaction. An admitted
group never starts a kache daemon; it is pointed at an already-running,
separately managed daemon, which the supervisor never starts, stops or owns.
kache's own scheduler is disabled with `KACHE_SCHEDULER=0`.

## Date

2026-10-26.

## Context and problem statement

buildlimits admits a compiler group into its own transient systemd scope under
`buildlimits-compilers.slice` and holds one token for it. The token returns
when the group empties. Everything else in the cache story follows from where a
cache miss runs.

With sccache the miss runs in `buildlimits-sccache.service`, outside the
admitted scope. The scope can therefore empty while admitted work continues in
the server, so `Admission` holds the group's token in `SERVER_DRAIN` until
`ManagedSccache.accounted_drain` observes the unit stopped with an empty
control group. Start-up quiesce, the `live_work`, `manager` and
`stopped_populated` faults, the `starved` trigger and the four-phase recovery
transaction all exist to make that hold safe and observable.

kache's process model is different in exactly that respect. Its wrapper runs
the compiler for a miss itself, so the compiler is a descendant of the group
leader and lives in the group's scope. If that holds for the versions in use,
then the sccache drain and recovery machinery has nothing to account for in
kache mode, and applying it would be actively wrong: it would hold tokens for a
server that carries no admitted work, and its "stop the cache" phases would
stop an operator's daemon.

kache brings two concerns sccache does not. It has an optional daemon that a
build can start, and it has its own machine-wide compile scheduler, which would
admit a second time inside a group buildlimits has already admitted.

The decision is therefore a contract for cache backends, a version range for
kache, a placement policy for kache's daemon, and a policy for kache's
scheduler. The governing decision to keep sccache in the admitted path is
dev-env-rocky ADR 011; this record extends that decision to a second backend
rather than replacing it. It answers [#30][issue-30] and supplies the design
the kache backend is built against.

### The capability that matters

A backend either runs cache-miss work **in the scope** or **detached** from it.
This is a property of the backend, not of the name `sccache`.

- **Detached.** Admitted work can outlive the scope. The group must hold its
  token until the detached work is accounted for, and the backend needs a
  verified-drain lifecycle.
- **In scope.** Every process a cache miss starts is a descendant of the group
  leader and dies with the scope. The group returns its token as soon as the
  scope empties, exactly as a direct group does.

The invariant this record adds is that `SERVER_DRAIN` requires both a composed
cache **and** a detached backend. Composition alone is not enough.

## Decision Drivers

- kache must be usable as a second backend without changing sccache behaviour,
  status output, metrics or snapshots.
- A kache group must never hold a token for work that cannot escape its scope.
- No kache daemon may remain in an admitted scope. The scope would never empty
  and the token would never return; cancellation would also kill a daemon the
  supervisor does not own.
- The supervisor must not take on ownership of a daemon that holds no admitted
  compile work.
- Admission must remain the only place that limits compile concurrency.
- The difference between backends must be a capability check, not a string
  comparison scattered across the supervisor.
- The in-scope claim must be evidence, not inference from a README.

## Requirements

### Functional requirements

- `cache_mode` accepts `kache`. Existing sccache keys keep their meaning, and
  a backend-specific key that does not belong to the selected mode is rejected
  with a clear message.
- Launch composes `kache <compiler argv…>`, preserving a workspace wrapper:
  `kache <wrapper> rustc <args…>`. A caller's own leading `kache` layer is
  stripped first, so the result is never `kache kache rustc`. A caller's leading
  `sccache` layer is stripped too: an admitted sccache inside a kache group
  would move the compiler into sccache's server and break the in-scope claim.
- A kache group returns its token when its scope empties and never enters
  `SERVER_DRAIN`, quiesce or recovery.
- Cancelling a kache group terminates the wrapper and its compiler children
  together inside the scope. kache's scheduler flocks are released by the
  kernel; no buildlimits bookkeeping is needed.
- If kache is unusable at admission, the compile runs directly and says so on
  standard error, in the same shape as the sccache fallback.
- Rustdoc stays `cacheable=False`, and the `rustc` probe queries exempted under
  [#27][issue-27] never go through kache.
- Status says `caching through kache` or
  `kache unavailable, compiling directly`. Metrics keep `cache_path` and add
  `cache_backend`. Detached-only events and the operator text that names the
  managed sccache service appear only for sccache.
- A process whose executable is `kache` or `kache-engine` that outlives its
  group's scope, or that sits in `buildlimits-compilers.slice` outside an
  admitted scope, is reported as its own fault, without holding tokens.

### Technical requirements

- A `CacheBackend` protocol covers argv and environment composition,
  reachability or health, an `in_scope_work` (or equivalent) capability, and
  the detached-only lifecycle (`accounted_drain`, `stop`, `start`). Exception
  names are backend-neutral.
- `ManagedSccache` implements the protocol with no behaviour change. Its
  status text, metrics and snapshots are byte-identical before and after the
  seam.
- Files stay under the 400-line limit from `AGENTS.md`, which requires
  splitting `sccache.py` and `cache_recovery.py`.
- No kache group carries `SCCACHE_*` variables; no sccache group carries
  `KACHE_*` variables.

## Options considered

### Option A: keep sccache as the only backend

No code change. dev-env-rocky's intended adoption of kache cannot happen, and
the second backend is re-litigated by the next operator. Rejected; the
capability split has to be recorded either way, so the cheapest correct moment
is before a second backend exists.

### Option B: treat kache as a detached backend

Reuse `SERVER_DRAIN`, quiesce and recovery for kache, on the theory that any
cache backend may leave work behind. This is false for both pinned versions
(see _On-host verification_), and it fails in two directions: tokens are held
while an empty scope waits for a server that carries no admitted work, and the
recovery transaction's stop phase targets a daemon the supervisor does not own.
Rejected.

### Option C: capability split with a backend seam

Introduce the `CacheBackend` protocol, mark kache `in_scope_work = True`, and
gate the drain, quiesce and recovery paths on the detached capability. kache
groups then follow the direct path for token release and cancellation, and
sccache keeps its existing path unchanged. Chosen, and delivered as a
behaviour-preserving seam PR before the kache backend lands.

### Option D: let a kache group auto-start its daemon inside the scope

Rely on kache's own session detaching to keep the daemon out of trouble.
Measured and rejected: on v2.0.0-rc.1 the auto-started daemon is created inside
the admitted scope and survives the wrapper, so the scope does not empty until
the supervisor terminates the group, and the daemon is then killed as an
orphan. See _The daemon policy_.

### Option E: give the supervisor ownership of a kache daemon

Have the supervisor start and stop a kache daemon unit, as it does for sccache.
Rejected: the daemon holds no admitted compile work, so there is nothing to
account for and nothing to quiesce. Owning it would import stop/start semantics
into the supervisor for no admission benefit, and would compete with the
operator's own management of that daemon.

| Topic                       | sccache (detached)                   | kache (in scope)            |
| --------------------------- | ------------------------------------ | --------------------------- |
| Miss compiler's parent      | the managed server, out of scope     | the wrapper, in the scope   |
| Scope empties while working | yes                                  | only when the group ends    |
| Token release               | `SERVER_DRAIN`, then accounted drain | on scope empty, as direct   |
| Start-up quiesce            | yes                                  | never                       |
| Recovery transaction        | `manual` or `starved`                | not applicable, inert key   |
| Supervisor owns a service   | yes, `buildlimits-sccache.service`   | never                       |
| Daemon                      | is the cache                         | external, unowned, required |
| Secondary scheduler         | none                                 | disabled                    |
| `cache_backend` metric      | `sccache`                            | `kache`                     |
| Detached-only events        | emitted                              | not emitted                 |
| Rustdoc                     | not cached                           | not cached                  |

_Table 1: What admission may assume about each backend._

## Decision outcome

### The backend seam

A `CacheBackend` protocol is owned by the shell that already holds
configuration and infrastructure, and the admission core learns its capability
as a projection: the supervisor sets, alongside the existing `cached` flag, a
single boolean saying that composed work may outlive the scope. `Admission`
then retains a token at `group_emptied` only when the group is cached **and**
the backend is detached **and** the ending may have left work behind. The
capability is read once per admission cycle from the backend, not compared as a
mode string.

`ManagedSccache` becomes one adapter with `in_scope_work = False`; a kache
adapter has `in_scope_work = True` and implements only composition and health.
The detached-only lifecycle members are never called for an in-scope backend,
and the recovery, quiesce and `SERVER_DRAIN` paths are unreachable in kache
mode by construction, not by a mode check at each call site.

Backend names, not mode strings, provide the journal and status wording.
sccache's wording, its metric values and its snapshots do not change.

### Configuration

| Key                  | Modes   | Meaning                                                    |
| -------------------- | ------- | ---------------------------------------------------------- |
| `cache_mode`         | all     | `sccache`, `kache` or `direct`                             |
| `sccache_executable` | sccache | The sccache client                                         |
| `sccache_unit`       | sccache | The managed server's user unit                             |
| `sccache_socket`     | sccache | The managed server's socket                                |
| `cache_recovery`     | sccache | `manual` or `starved`; inert in other modes                |
| `kache_executable`   | kache   | The kache client; see the resolution rule below            |
| `kache_socket`       | kache   | The externally managed daemon's socket, if not the default |
| `kache_cache_dir`    | kache   | The store, if not kache's own default                      |

_Table 2: Configuration by mode._

Every key is optional, so "invalid for the mode" is decided by the keys the
operator actually wrote, not by a difference from the defaults. Writing
`sccache_unit` while `cache_mode = "kache"`, or `kache_socket` while it is
`"sccache"`, is an error naming the key and the mode, because the value would
otherwise look accepted and do nothing. `kache_socket` is exported to the group
as `KACHE_SOCKET_PATH`; when it is unset the group exports nothing and health
asks kache to resolve its own default socket. `KACHE_SOCKET_PATH` is an
operational variable, so `[cache] ignore_env` in a host kache configuration
does not suppress it.

`kache_cache_dir` is the store half of the same argument. Health checks the
directory it was configured with, so the group must write that directory, or
the probe would verify one filesystem while builds filled another. When the key
is set the group exports `KACHE_CACHE_DIR`; when it is unset the group exports
nothing and health cannot check a path it was never told, leaving the store to
kache's own resolution under `$XDG_CACHE_HOME/kache` or `~/.cache/kache`.

`kache_executable` may be an absolute path or a bare name. A bare name is
resolved once, on the supervisor's own `PATH`, and the resolved file is what
the health probe runs and what composition puts in the group's argv. Resolving
once is what keeps the two from disagreeing: `launch.py` executes the composed
vector with the requesting build's environment, so a name left in argv would be
searched a second time against a different `PATH`. That could admit a group
whose client the build cannot find, or reject a client it could. A supervisor
started by systemd has a minimal `PATH`, so a host that installs kache outside
the usual directories should configure an absolute path.

### Supported versions and their process models

Two versions are supported, both verified on this host:

| Version      | Architecture                                               |
| ------------ | ---------------------------------------------------------- |
| `1.2.2`      | one `kache` binary, used directly as `RUSTC_WRAPPER`       |
| `2.0.0-rc.1` | a thin `kache` client that `exec`s `kache-engine` in place |

_Table 3: Supported kache versions._

Other versions are unsupported until the verification in _On-host verification_
is repeated for them. That is deliberate: kache's cache-key recipes and private
storage layouts may change within a major series, and its release cadence is
weekly.

The v2 client and engine share one process: the client `exec`s
`kache-engine --kache-forwarded-argv-v1 <version> <micros> <client path>
<identity> <args…>`,
so a snapshot may see either executable at the wrapper's process identifier.
Both are recognized as wrapper processes.

Neither version runs a miss compiler in its daemon, so no version needs to be
excluded from the supported range:

- v1.2.2 has no daemon-execution path. The wrapper starts the compiler with
  `std::process::Command::new(&args.rustc)` (`src/wrapper.rs:7755`).
- v2.0.0-rc.1 lets the daemon ask a trusted client to execute a compile, but
  the client still spawns the compiler as its own child, with
  `command.process_group(0)` and a Linux `pre_exec` `PR_SET_PDEATHSIG` bound to
  the client (`crates/kache-ipc/src/execution/unix_client.rs:260,266`).
  Delegation is refused unless both peers may delegate and the pipeline is
  trusted (`src/daemon_execution.rs:29`), and `trust_daemon_execution` defaults
  to off. The parentage and survival evidence below was measured with the
  default, so the trusted path is established from source rather than
  measurement, and is recorded as a limitation.

### The daemon policy

No kache configuration key disables automatic daemon start in v2.0.0-rc.1. The
daemon's own configuration surface is idle timeout, publish, trust and
execution concurrency; `KACHE_DISABLED=1` stops caching altogether and is not a
candidate. `KACHE_DAEMON_IDLE_TIMEOUT` only ends an idle daemon.

The policy is therefore the alternative the issue allows: **an admitted group
is pointed at an already-running, separately managed daemon.** The group
receives `KACHE_SOCKET_PATH`, which is an operational variable and so is not
suppressed by `[cache] ignore_env`, and the auto-start branch is not reached
because `daemon::is_running` is true. Measured on v2.0.0-rc.1: with a daemon
listening, no daemon process is created in the scope; without one, an in-scope
`kache-engine daemon run` is created with the wrapper as its parent, and it
survives the wrapper's `SIGKILL`.

The supervisor's obligations are then:

- It never starts, stops, restarts or owns the daemon, and it never installs a
  unit for it. The socket path is a configuration value only.
- Health at admission is the executable, the cache directory, and
  `kache daemon status --json` naming the configured socket with
  `daemon_running: true`. That subcommand never starts a daemon and exits zero
  with `daemon_running: false` when none is running, so it is safe to call on
  every admission cycle. The executable health runs is the one resolution of
  `kache_executable` described under Configuration, and it is that same
  resolved file that the composed argv names. The directory health checks is
  `kache_cache_dir`, and the group it admits exports that same directory as
  `KACHE_CACHE_DIR`; without the key the directory is kache's own and health
  cannot check it.
- If health fails, the compile runs directly, with the notice on standard
  error and `direct_fallback` in `cache_path`.
- A `kache` or `kache-engine` process found in `buildlimits-compilers.slice`
  outside an admitted scope, or one that outlives its group's scope, is
  reported as a distinct fault. Status carries the identifiers in
  `stray_kache_processes`, always present so that a reader can tell "no strays"
  from "not guarded". A change in the reading also writes a plain journal line,
  which begins `stray kache process outside admitted scopes:` and is journalled
  once per appearance or clearance rather than once per poll. The fault never
  holds a token. The in-scope case cannot normally arise because the external
  daemon is required, but the guard covers a daemon that dies mid-build and a
  group that then auto-starts a replacement.

The guard is a report, not a killer. A stray process inside a group's scope is
removed by the ordinary terminate-on-leader-exit path; a stray process outside
every scope is the operator's to remove, and status names it.

### The scheduler policy

`KACHE_SCHEDULER=0` is set in every admitted kache group, and
`KACHE_LOCAL_ONLY=1` with it, which also makes v1.2.2's remote-gated daemon
start paths inert.

With the scheduler disabled, kache keeps its per-key build claim
(`store.claim_build`) but loses its flight sharing: `admit_scheduler_miss`
returns immediately, and `discovery_flight_dir` is `None`. Same-key misses in
two worktrees are then two real compiles, with the slower one waiting on the
claim for at most `BUILD_LOCK_TIMEOUT = 600 s` instead of being served the
first one's result. Also lost: the machine-wide permit pool sized at
`available_parallelism()`, the 512 MiB RSS weighting per slot, the whole-pool
request under memory pressure, and the 30-minute fail-open. The last two are
the reason the scheduler must not be enabled inside an admitted group: a
whole-pool request would admit a second time under a pressure signal
(`PSI_SOME_AVG10 = 10.0`) read from the group's own cgroup, and the fail-open
would let a compile proceed after 1800 s of waiting, both invisible to
buildlimits' pool accounting. The duplicate misses are the price of a single
admission layer, and they are bounded by Cargo's own unit graph rather than by
the number of worktrees, since only identical keys collide.

An operator who re-enables the scheduler on such a host gets both layers. The
users' guide records that interaction with the other kache settings when the
backend lands, as step 5 of the migration plan requires.

### What recovery means in each mode

`cache_recovery` is a policy for a detached backend: it decides when the
supervisor may interrupt the managed sccache service to unblock a waiting
request. In kache mode there is no managed service and no retained-drain token,
so it has nothing to decide. It stays accepted and inert rather than being
rejected, because `starved` is the default and rejecting it would make the
default configuration invalid for every kache host; rejecting only an explicit
`starved` would in turn require the loader to distinguish a written value from
a defaulted one.

The policy is therefore: the key is accepted in kache mode, has no effect, and
status never mentions it. `build-limits-recover` answers `no-cache-recovery`,
the same refusal it already gives in direct mode, and says the supervisor
manages no sccache service.

Status says so: no recovery phase, no drain fault and no recovery action line
is rendered in kache mode, because none of those states can be entered.

### Status, metrics and journal

| Surface           | sccache                                   | kache                                   | direct               |
| ----------------- | ----------------------------------------- | --------------------------------------- | -------------------- |
| Status summary    | `caching through sccache`                 | `caching through kache`                 | `compiling directly` |
| Unavailable text  | `sccache unreachable, compiling directly` | `kache unavailable, compiling directly` | not shown            |
| `cache_path`      | `cached`, `direct_fallback`, `direct`     | same                                    | `direct`             |
| `cache_backend`   | `sccache`                                 | `kache`                                 | `direct`             |
| Detached events   | emitted                                   | not emitted                             | not emitted          |
| Recovery guidance | names the managed sccache service         | never shown                             | never shown          |

_Table 4: Backend-named surfaces._

Existing status JSON fields keep their meaning; `cache_backend` is additive.
`cache_path` keeps its meaning as the outcome of a request, which is why the
backend is a separate field: `direct_fallback` says a cache was wanted and not
used, and only `cache_backend` says which cache.

### On-host verification

Measured on 2026-10-26 on `rohga.df12.net` (Rocky 10, systemd 257,
`rustc 1.99.1`, commit `58cf8f9c35b355199727627d07ae34a4e2846d67`, 24 CPUs).
Each case ran a real `rustc` compile of a slow crate as the kache wrapper's
argument, inside a transient scope created with `systemd-run --user --scope`,
with `KACHE_SCHEDULER=0` and `KACHE_LOCAL_ONLY=1`. The wrapper was killed with
`SIGKILL`; the scope was then killed through the manager, which is the same
operation the supervisor performs.

Binaries:

```plaintext
v1.2.2      kache        49cb84258385c2a9c36157d38ed01ad24c5a681b02b3ded5edb70476b3f966a6
v2.0.0-rc.1 kache        65a9b2c30063281de28d8c7f2829f3342757fbd17841ea83028c8dbee9ba97f0
v2.0.0-rc.1 kache-engine 323cf724f33f1103d04e9399a683958078df5c101d0833a5faa8c0417c3e03b8
```

#### Case 1: v1.2.2 with no daemon

```plaintext
scope      bltverify-v1-nodaemon-1523639.scope
wrapper    pid 1523641  ppid 1523639  exe bin/v1/kache
compiler   pid 1523702  ppid 1523641  exe .../bin/rustc
           cgroup .../bltverify-v1-nodaemon-1523639.scope
descends from wrapper: true    shares wrapper cgroup: true
scope members while compiling: 1523641, 1523702

after SIGKILL of the wrapper 1523641
compiler   1523702 survives, ppid 463000, still in the scope
kache      none
scope members: 1523702

after killing the scope
compiler   none      kache   none      scope members: none
```

#### Case 2: v1.2.2 with an external daemon

```plaintext
daemon     pid 1537563  argv kache daemon run
           cgroup .../bltverify-v1-external-1537548-daemon.service
status     daemon_running: true   daemon_version: "1.2.2"
           socket: .../run-v1-external/runtime/daemon.sock
wrapper    pid 1537611  ppid 1537548  exe bin/v1/kache
compiler   pid 1537727  ppid 1537611  exe .../bin/rustc
descends from wrapper: true    shares wrapper cgroup: true
scope members while compiling: 1537611, 1537727

after SIGKILL of the wrapper 1537611
compiler   1537727 survives, ppid 463000, still in the scope
kache      1537563, still in its own daemon unit
scope members: 1537727

after killing the scope
compiler   none      scope members: none
kache      1537563, still in its own daemon unit
```

#### Case 3: v2.0.0-rc.1 with no daemon

```plaintext
scope      bltverify-v2-nodaemon-1524831.scope
wrapper    pid 1524832  ppid 1524831  exe bin/v2/kache-engine
           argv kache-engine --kache-forwarded-argv-v1 2.0.0-rc.1 507 ... kache ...
compiler   pid 1524933  ppid 1524832  exe .../bin/rustc
descends from wrapper: true    shares wrapper cgroup: true
auto-started daemon
           1524836  ppid 1524832  argv kache-engine daemon run
           in bltverify-v2-nodaemon-1524831.scope
scope members while compiling: 1524832, 1524836, 1524933

after SIGKILL of the wrapper 1524832
compiler   none: the compiler died with its parent (PR_SET_PDEATHSIG)
kache      1524836 survives, ppid 463000, still in the scope
scope members: 1524836

after killing the scope
compiler   none      kache   none      scope members: none
```

#### Case 4: v2.0.0-rc.1 with an external daemon

```plaintext
daemon     pid 1529436  argv kache-engine --kache-forwarded-argv-v1 ... kache daemon run
           cgroup .../bltverify-v2-external-1529419-daemon.service
status     daemon_running: true   daemon_version: "2.0.0-rc.1"
           socket: .../run-v2-external/runtime/daemon.sock
wrapper    pid 1529481  ppid 1529419  exe bin/v2/kache
compiler   pid 1529554  ppid 1529481  exe .../bin/rustc
descends from wrapper: true    shares wrapper cgroup: true
scope members while compiling: 1529481, 1529554
in-scope daemon: none

after SIGKILL of the wrapper 1529481
compiler   none      kache   none      scope members: none

after killing the scope
compiler   none      scope members: none
kache      1529436, still in its own daemon unit
```

_Table 5: The four probe cases. Every case asserts the two claims the design
rests on: the miss compiler's parent is the wrapper and the compiler shares the
wrapper's cgroup, and no compiler process survives outside the scope._

What the four cases establish, and what they narrow:

- The in-scope claim holds for both versions, with and without a daemon.
- Killing the wrapper is not enough to end a miss on v1.2.2. The compiler
  continues in the scope until the scope itself is killed, which the supervisor
  already does on leader exit and on cancellation. On v2.0.0-rc.1 the compiler
  also dies immediately, because the client sets `PR_SET_PDEATHSIG`; that is
  additional safety, not the mechanism relied on.
- v2.0.0-rc.1 auto-starts its daemon **inside** the admitted scope, and the
  daemon outlives the wrapper. This is the measurement that rules out Option D
  and forces the external-daemon policy. v1.2.2 does not auto-start a daemon
  here at all, because its start paths are reachable only with a remote
  configured.
- An in-scope daemon does not survive the scope kill, so the ordinary
  terminate path contains it even when the external daemon policy fails.

### How RFC 0001 and RFC 0002 apply

RFC 0001 models admission and recovery together. Its fairness and safety tables
are mostly backend-independent: F1, F2 and F5 to F7, and S1, S2, S4, S5 and S7
to S9, are statements about tokens, barriers and observations and hold
unchanged. Three entries are claims about sccache and are not instantiated in
kache mode:

- **F3, "detached server work finishes or is stopped"** is, as RFC 0001 itself
  notes, a claim about sccache rather than buildlimits.
- **S3, "accounted release"**, constrains the release of a `SERVER_DRAIN`
  token. kache mode never creates one.
- **S6, "server work coverage"**, bounds live detached work by the tokens held
  by cached groups. There is no detached work to bound.

kache mode reuses direct mode's token release, which is the intended outcome
recorded here. The model needs one new dimension to say so: a group can be
cached and in scope, and such a group neither enters `SERVER_DRAIN` nor is
subject to the cache hold. The starvation incidents RFC 0001 analyses cannot
arise in kache mode, because the retained-drain token that caused them does not
exist there. RFC 0001 therefore gains a short amendment note, delivered with
the rest of the documentation in step 5 of the migration plan; its properties
are not rewritten.

RFC 0002 records hit rates and predicts hits for sccache 0.14.0 specifically.
Its counters and its `cache_path` vocabulary are already backend-neutral, but
its data source (`status.queued_requests`, sccache's own statistics), its
analysis and its predictive model are not. It does not apply to kache. A kache
successor would need kache's own hit signal and is out of scope for this
record. Step 5 adds a pointer to RFC 0002 saying it is sccache-specific, so a
reader does not carry its conclusions across to kache.

## Goals and non-goals

- Goals:
  - Admit kache without weakening token conservation or scope containment.
  - Make the detached capability the only trigger for drain, quiesce and
    recovery.
  - Keep sccache behaviour, status, metrics and snapshots unchanged.
  - Record the daemon and scheduler policies and the evidence behind them.
- Non-goals:
  - Provisioning: installing kache, its daemon unit, cache size and location,
    and switching hosts over. That is a dev-env-rocky issue.
  - Cache-hit prediction for kache.
  - kache's remote backends (S3, GCS, OCI) and its C/C++ and CUDA caching.
  - Owning or supervising a kache daemon.

## Migration plan

1. **Record the decision.** This document, indexed from
   [Documentation contents](contents.md).
2. **Extract the backend seam.** Add the `CacheBackend` protocol,
   backend-neutral exception names, the capability projection into `Admission`,
   backend-named wording, and the `sccache.py` and `cache_recovery.py` splits.
   Land the extraction as its own behaviour-preserving pull request, with
   sccache status, metrics and snapshots unchanged.
3. **Add the kache backend.** Configuration and per-mode key validation,
   composition and environment, the caller's `kache` and `sccache` layer
   stripping, health and the external-daemon requirement, the stray-process
   guard, `cache_backend` in the metrics vocabulary, and the backend-named
   status and journal text.
4. **Test.** `tests/fixtures/fake_kache.py` injected through configuration as
   `fake_sccache.py` is, with an optional long-lived daemon child. Hypothesis
   properties: a kache group's token is returned if and only if its scope is
   empty, and kache mode never enters `SERVER_DRAIN` or starts a recovery
   transaction. Composition table tests over the four wrapper shapes.
   Behaviour-driven systemd scenarios for a completed build, a cancelled group,
   an unavailable executable and the daemon guard.
5. **Document.** Users' guide, developers' guide, `README.md`, ADR 002's note
   on unaffected caches, the RFC 0001 amendment, the RFC 0002 pointer, and
   `docs/migrating-from-dev-env-rocky.md`.
6. **Verify.** `make lint`, `make typecheck`, `make test`, `make markdownlint`
   and `make nixie`, then review. Provisioning follows in the dev-env-rocky
   repository.

## Known risks and limitations

- **A host or project kache configuration with `[cache] ignore_env = true`**
  suppresses the file-backed environment variables, which includes
  `KACHE_SCHEDULER` and `KACHE_LOCAL_ONLY`. `KACHE_SOCKET_PATH` is operational
  and exempt, so the placement policy still holds, but the second admission
  layer could return unnoticed. The users' guide forbids `ignore_env` on hosts
  that compose kache, and status cannot detect the setting.
- **The external daemon becomes a prerequisite.** A host with no running
  daemon compiles directly, with the notice, rather than caching. That is the
  safe failure but it is a visible one, and the provisioning issue must install
  and enable the unit.
- **The v2 trusted-delegation path is established from source, not
  measurement.** The measured cases use the default
  `trust_daemon_execution = false`. Source shows the compiler is still spawned
  by the client, with `PR_SET_PDEATHSIG`, when trust is on. A future version
  that moves the spawn into the daemon would be a detached backend and must be
  excluded from the supported range.
- **Version support is per-version, not a semver range.** kache may change its
  cache-key recipe within 1.x, so the in-scope claim is re-verified before a
  version is added.
- **Duplicate same-key misses** across worktrees are accepted with
  `KACHE_SCHEDULER=0`, bounded by `BUILD_LOCK_TIMEOUT` for the waiting side.
- **The stray-process guard is observational.** A stray kache process outside
  every scope is reported, not removed.
- **`cache_recovery` being inert** means a shared configuration file says
  something that has no effect in kache mode. Status is the only surface that
  tells the operator.

## Outstanding decisions

- Whether a kache-specific successor to RFC 0002 is worth its own record.
- Whether a later kache version offers a supported switch to disable daemon
  auto-start, which would relax the external-daemon requirement to an
  optimization.
- Whether the stray-process guard should ever terminate what it finds outside
  an admitted scope, or remain a report.

## Architectural rationale

The change follows the same boundary the supervisor already draws: a pure
admission core and an imperative shell. The backend is a driven port — argv and
environment composition, health, and, for detached backends only, a
verified-drain lifecycle — and the core sees only a capability, never a
backend. sccache and kache are adapters on that port.

Capability rather than identity is what keeps the code honest as backends
multiply. A name comparison at each call site would have to be revisited for
every backend and would silently default a new one to "detached", which holds
tokens for work that cannot escape. `in_scope_work` defaults to nothing: a
backend must state which it is.

The daemon and scheduler policies both come from the same principle.
buildlimits owns admission of compilations, and it owns nothing that is not
admitted compilation. That is why an unowned daemon is pointed at rather than
managed, and why kache's scheduler is disabled rather than tuned: any second
admission layer inside an admitted group makes the pool's accounting a partial
truth.

[issue-27]: https://github.com/leynos/buildlimits/issues/27
[issue-30]: https://github.com/leynos/buildlimits/issues/30
