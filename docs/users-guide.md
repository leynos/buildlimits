# buildlimits user's guide

This guide is for operators of a shared development host and for anyone whose
Cargo builds run through compile admission. It explains what buildlimits does,
how to install and configure it, what a build sees while it waits, and how to
read status, recover a retained cache and interpret metrics. Contributor
workflows live in the [developer's guide](developers-guide.md).

## What buildlimits does

buildlimits limits how many Cargo compiler groups run at once across a whole
machine — for every agent, repository and worktree — without changing any build
command. A [guildmaster](https://codeberg.org/amonakov/guildmaster) token pool
at `/dev/guild` holds the machine-wide budget. Each user runs one admission
supervisor, which takes one token per compiler group and returns it only when
every process of that group has exited. Compilations still go through sccache
when it is available.

Cargo reaches the supervisor through two small adapters that it already knows
how to call: one stands in as `RUSTC_WRAPPER`, the other as `RUSTDOC`. Nested
compilations inside an admitted group, such as the ones rustdoc starts, run
directly and never ask for a second token.

| Command                   | Role                                                     |
| ------------------------- | -------------------------------------------------------- |
| `build-limits-rustc`      | `RUSTC_WRAPPER`: runs one compiler under admission       |
| `build-limits-rustdoc`    | `RUSTDOC`: runs the toolchain's rustdoc under admission  |
| `build-limits-supervisor` | The per-user admission supervisor, run as a user service |
| `build-limits-status`     | Prints capacity, held tokens, the queue and groups       |
| `build-limits-recover`    | Runs or inspects a managed-cache recovery transaction    |
| `build-limits-metrics`    | Summarizes bounded compile-admission journal metrics     |

_Table 1: Installed commands._

## Requirements

- Linux with cgroup v2 and a systemd user manager reachable through `busctl`.
  Each admitted compiler group runs in its own transient scope under
  `buildlimits-compilers.slice`.
- A guildmaster token pool at `/dev/guild`, readable and writable by the user's
  `guild` group membership.
- Python 3.14 or newer. Admission uses the standard library; the OpenTelemetry
  SDK is the runtime dependency for tracing.
- Optionally, sccache, run as the managed `buildlimits-sccache.service` user
  unit when the supervisor composes compilers with the cache.

## Installation

The package installs as an ordinary Python distribution with six console
scripts. The [uv](https://docs.astral.sh/uv/) tool installer keeps it isolated
from project environments:

```bash
uv tool install --python 3.14 git+https://github.com/leynos/buildlimits
```

On managed hosts, the `devhost.buildlimits` Ansible collection in
[dev-env-rocky](https://github.com/leynos/dev-env-rocky) provisions everything
around the package: the guildmaster pool and its group membership, the
`supervisor.toml` file, the `buildlimits.slice`, `buildlimits-supervisor` and
`buildlimits-sccache` user units, and the handover of Cargo's wrapper settings.
The collection's role documentation covers provisioning switches, staging and
the maintenance procedures that need privileged access.

## Quick start

With the pool present and the supervisor's user unit installed, point Cargo at
the adapters in `~/.cargo/config.toml`:

```toml
[build]
rustc-wrapper = "build-limits-rustc"
rustdoc = "build-limits-rustdoc"
```

Start the supervisor and confirm that it is healthy:

```bash
systemctl --user start buildlimits-supervisor.service
build-limits-status
```

```plaintext
capacity 2, held 0, healthy, caching through sccache
```

Any `cargo build`, `cargo test` or `cargo doc` now waits for a slot when the
machine is busy, and runs as soon as one is free.

## Configuration

### Supervisor settings

The supervisor reads `~/.config/buildlimits/supervisor.toml`, or the file named
by `build-limits-supervisor --config PATH`. Every key is optional; a missing
file yields the defaults. Unknown keys and wrongly typed values stop start-up
with exit status 2, and nothing is half-applied.

| Key                     | Default                                        | Meaning                                                                                                                                    |
| ----------------------- | ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `device`                | `/dev/guild`                                   | The guildmaster token device                                                                                                               |
| `slots`                 | `2`                                            | The pool's configured capacity, reported by status                                                                                         |
| `socket_path`           | `$XDG_RUNTIME_DIR/buildlimits/supervisor.sock` | Where the supervisor listens                                                                                                               |
| `lock_path`             | `$XDG_RUNTIME_DIR/buildlimits/supervisor.lock` | The singleton lock                                                                                                                         |
| `compilers_slice`       | `buildlimits-compilers.slice`                  | The slice that holds every compiler scope; must end in `.slice`                                                                            |
| `cancel_grace_seconds`  | `10.0`                                         | Seconds between `SIGTERM` and `SIGKILL` for a terminated group                                                                             |
| `drain_timeout_seconds` | `120.0`                                        | The longest start-up waits for surviving groups to drain                                                                                   |
| `cache_mode`            | `sccache`                                      | `sccache` composes admitted compilers with the managed cache; `direct` runs them without it, and recovery then answers `no-cache-recovery` |
| `sccache_executable`    | `sccache`                                      | The sccache client used in `sccache` mode                                                                                                  |
| `sccache_unit`          | `buildlimits-sccache.service`                  | The managed cache's user unit                                                                                                              |
| `sccache_socket`        | `$XDG_RUNTIME_DIR/buildlimits/sccache.sock`    | The managed cache's socket                                                                                                                 |
| `cache_recovery`        | `starved`                                      | `starved` or `manual`; see [Recover a retained managed cache](#recover-a-retained-managed-cache)                                           |
| `recovery_marker`       | `$XDG_RUNTIME_DIR/buildlimits/cache-recovery`  | The restart barrier the managed cache unit honours                                                                                         |

_Table 2: Supervisor configuration keys._

Sockets live under `$XDG_RUNTIME_DIR/buildlimits`, falling back to
`/run/user/UID/buildlimits` when the variable is unset, because Unix socket
paths are limited to about 108 bytes.

```toml
slots = 4
cache_mode = "sccache"
cache_recovery = "manual"
```

### Adapter environment

The adapters read only their environment, so the same settings work for Cargo,
agents and shells alike.

| Variable                        | Default                         | Effect                                                  |
| ------------------------------- | ------------------------------- | ------------------------------------------------------- |
| `BUILD_LIMITS_SOCKET`           | the supervisor's default socket | Overrides the socket the adapters connect to            |
| `BUILD_LIMITS_UNAVAILABLE_SECS` | `300`                           | How long an adapter waits for an unreachable supervisor |
| `BUILD_LIMITS_HEARTBEAT_SECS`   | `RUSTC_HEARTBEAT_SECS`, or `45` | Interval between waiting and compiling progress lines   |
| `BUILD_LIMITS_RUSTUP`           | `rustup` on `PATH`              | The rustup used to resolve the real rustdoc             |
| `BUILD_LIMITS_COMPILERS_SLICE`  | `buildlimits-compilers.slice`   | The slice whose scopes count as already admitted        |

_Table 3: Adapter environment variables._

The two duration variables take seconds. A value that does not parse, or is not
a finite positive number, is ignored as if it were unset, so a mistyped setting
falls back to the default instead of failing the build.

## What a build sees

A build that waits for a slot prints one line to standard error, repeated at
the heartbeat interval:

```plaintext
[build-limits] serde: waiting for a compile slot (1 ahead, 45s)
```

A long compilation prints `[rustc-heartbeat]` lines instead. The compiler
itself writes to the caller's own standard streams, received as passed file
descriptors, so Cargo's JSON diagnostics reach Cargo unchanged. When the
managed sccache server is down, a build still runs, uncached, and says so on
standard error. The adapter exits with the compiler's exit code, or ends itself
with the same signal; a signal to the adapter closes its connection, and the
supervisor then terminates that compiler group.

Run each worktree's gates sequentially within that worktree. Independent
repositories and worktrees may run their gates concurrently: start each gate
once and let Cargo's package-cache lock and compile admission queue the work. A
healthy pool with every slot held is normal operation. Check progress with
`build-limits-status` rather than waiting for the whole host to go idle before
submitting work.

### Descriptor pressure

When the supervisor is short of file descriptors before it has recorded a
request, it answers `busy`. The adapter retries that refusal with bounded,
jittered backoff under the same unavailability deadline and prints one waiting
line. Once a request is queued, it keeps reporting progress and stays
cancellable. A persistent refusal is an infrastructure error: the adapter never
bypasses admission to run a compiler directly.

A request whose complete, valid declaration needs more descriptors than the
known total budget receives a final `descriptors` refusal naming the required
count and the budget. Temporary lack of headroom, an unknown budget, or a
truncated declaration that would still fit remain retryable.

If the supervisor cannot accept a connection because of descriptor pressure, it
pauses only its listener and retries on a later tick. The listening socket and
its kernel backlog stay in place, so new arrivals wait while existing requests
and compiler groups continue. This backpressure does not mark admission
unhealthy.

### A real compiler that cannot find libLLVM

A toolchain that ships `rustc-dev` keeps a second `librustc_driver` in
`lib/rustlib/<triple>/lib`. Cargo puts that directory on `LD_LIBRARY_PATH` for
build scripts. If the real `rustc` is run with that value, the loader binds the
duplicate driver, whose `$ORIGIN/../lib` has no `libLLVM`, and the compiler
exits with status 127 before it prints anything. Kani 0.67.0 drives such a
toolchain, `nightly-2025-11-21`. [ADR 002](adr-002-toolchain-library-path.md)
records the cause and the decision.

**Recognize the symptom.** A build script such as `rustversion`'s reads only
standard output, so the failure appears as an empty version rather than as a
missing library:

```plaintext
Error: unexpected output from `rustc --version`: ""

Please file an issue in https://github.com/dtolnay/rustversion
```

**Probe the toolchain Kani drives**, not the repository's own pin, which has no
duplicate and always passes. The probe loads the compiler and compiles nothing,
so it does not need a compile slot:

```bash
KT=$HOME/.rustup/toolchains/nightly-2025-11-21-x86_64-unknown-linux-gnu
RL=$KT/lib/rustlib/x86_64-unknown-linux-gnu/lib
ls "$KT"/lib/librustc_driver-*.so "$RL"/librustc_driver-*.so   # two copies?
ls "$RL"/libLLVM* 2>/dev/null || echo "no libLLVM beside the duplicate"
LD_LIBRARY_PATH=$RL "$KT/bin/rustc" --version; echo "rc=$?"          # 127 if affected
LD_LIBRARY_PATH=$RL:$KT/lib "$KT/bin/rustc" --version; echo "rc=$?"  # 0
```

A duplicate alone does not prove the failure: several `rustc-dev` toolchains
also keep a `libLLVM` in the rustlib directory and start normally. Only a
`rc=127` from the first probe line does.

**What the adapters do.** When `build-limits-rustc` or `build-limits-rustdoc`
runs a real toolchain `rustc` or `rustdoc` (`<root>/bin/rustc` with a
`librustc_driver-*.so` in `<root>/lib`), it prefixes `<root>/lib` to a non-empty
`LD_LIBRARY_PATH` that does not already list it, as the rustup proxy does.
This applies to a compiler admitted by the supervisor and to one the adapter
runs directly inside an admitted scope. Arguments are never changed, and the
proxy, `clippy-driver` and unrecognized compilers see the environment
unchanged. No operator action is needed. Processes that were already running
keep their old environment.

**What they cannot do.** A build script that Cargo runs, and anything else that
starts a real `rustc` without these adapters, still inherits the poisoned path.
For a Kani-gated suite, put the Kani toolchain's `lib` directory on the path
before running it:

```bash
KT=$HOME/.rustup/toolchains/nightly-2025-11-21-x86_64-unknown-linux-gnu
export LD_LIBRARY_PATH="$KT/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
make test
```

The entry works because Cargo appends the inherited value to the rustlib
directory, and the loader finds `libLLVM` through any listed directory. The
workaround belongs to one toolchain, so repeat the probe and change `KT` when a
Kani upgrade changes the pinned toolchain. Test guards that look only for
`error while loading shared libraries`, such as the one in `theoremc`, do not
match this failure.

## Status

```bash
build-limits-status
build-limits-status --json
build-limits-status --ready
build-limits-status --ready --json
journalctl --user --unit buildlimits-supervisor.service
```

`build-limits-status` shows the capacity, held tokens, waiting requests and
running compiler groups. Guildmaster reports none of these, so the status comes
from the supervisor. The plain-text output also explains an unhealthy state,
the current drain fault and any required recovery action. The `--json` document
adds descriptor limits, the open-descriptor sample, request-owned retention,
the high-water mark and the budget. These are point-in-time diagnostics, not
history; `build-limits-metrics` summarizes history.

| Exit status | Default: health check                         | With `--ready`                                  |
| ----------- | --------------------------------------------- | ----------------------------------------------- |
| `0`         | Query succeeded and the supervisor is healthy | Healthy and `admission.ready` is exactly `true` |
| `1`         | Query or health failure                       | Query or non-truthy health failure              |
| `3`         | Not used                                      | Readiness is false, missing or malformed        |

_Table 4: `build-limits-status` exit statuses._

Success without `--ready`, or `healthy` alone, is not evidence of usable
admission readiness. The status fields describe separate signals:

- `healthy` reports supervisor and token-pool health. It does not say whether
  admission can make progress.
- `caching` reports whether the managed cache was reachable at its last probe.
  Cache reachability does not determine readiness; direct compilation can be
  admitted while sccache is unavailable.
- `restarting` says the supervisor is winding down for a graceful stop. It can
  remain healthy while running groups finish.
- `drain_fault` gives the latest failed drain-accounting check; its retained
  tokens stay held until accounting succeeds. `drain_fault_kind` classifies the
  failure as `live_work`, `stopped_populated` or `manager`.
- `drain_attempts` counts failed drain checks and resets when the drain fault
  clears.
- `recovery_action` gives operator guidance once the retry threshold is reached.
  `recovery_action_is_fallback` is true when that command is only a fallback
  because automatic recovery is pending. This guidance can be delayed and can
  change independently of admission classification, which uses the immediate
  fault and current recovery outlook.
- `admission.state` is the readiness classification shown below.
  `admission.ready` is true only for `ready`, `saturated` or `draining`.

Each status document is a point-in-time snapshot. Readiness may change after a
query and does not prove that a monitoring consumer observed or acted on it.

Plain status prepends `admission <state>: <reason>` when the state is `blocked`,
`draining`, `closing` or `unhealthy`; `ready` and `saturated` add no line.
Scripts that inspect the first line must account for this prefix. For
provisioning and monitoring, use `--ready` or the JSON readiness field as shown
below.

`--ready` uses the same plain-text or JSON output with the exit-status contract
in Table 4. Use it for provisioning checks and monitoring that need usable
admission:

```bash
build-limits-status --ready
build-limits-status --json | jq -e '.admission.ready == true'
```

For an admission-specific readiness check, inspect `admission` in the JSON
document. Its `state` is one of the values below, `ready` says whether
admission has a current progress path, and `reason` gives a short count-only
explanation without request or group identifiers. `running`, `draining` and
`server_drain` are compiler-group counts from the ledger; `queued` is the
current admission-queue length, even before requester metadata is available.

| State       | `ready` | Meaning                                                                 |
| ----------- | ------- | ----------------------------------------------------------------------- |
| `ready`     | `true`  | The ledger reports capacity available for a compiler group.             |
| `saturated` | `true`  | All slots are occupied; admission can continue when a slot is released. |
| `draining`  | `true`  | Every slot is retained in server-drain, with a current progress path.   |
| `blocked`   | `false` | Every slot is retained in server-drain and operator recovery is needed. |
| `closing`   | `false` | The supervisor is winding down.                                         |
| `unhealthy` | `false` | The supervisor or its token pool is unhealthy.                          |

`draining` reports a present progress path, not a guarantee of eventual
progress. The readiness result is independent of cache reachability: sccache
may be unavailable while direct compilation can still be admitted. A `true`
result does not promise that a queued compile can start immediately; a
`saturated` pool still has to wait for current compiler work to release a slot.
Partial server-drain retention remains `ready` when capacity is available or
`saturated` when all slots are occupied; it is not `draining`. Readiness
failure is an operational signal; it never authorizes releasing tokens or
stopping sccache. Retained tokens remain held until verified stopped-and-empty
accounting permits their release.

Under `descriptors`, `open` is the sampled count of open descriptors, or `null`
with a numeric `sampling_errno` when enumeration failed; the text summary says
`unavailable`. Limits of `-1` mean unlimited. These gauges help track headroom;
they do not prove that descriptors are leak-free.

### Start-up refusal

Before it opens the pool, the supervisor requires the managed sccache service
to be stopped and its control group empty, because a live server with no
visible compiler children may still hold accepted work waiting for a Cargo
jobserver token. A live service does not stop start-up: the supervisor has by
then terminated every compiler scope, so no admitted group can still use the
cache, and it stops the service itself under the restart barrier (see
[Restart the supervisor after an upgrade](#restart-the-supervisor-after-an-upgrade)).

Start-up still exits with status 2 before the status socket opens when it
cannot prove the service stopped and empty: the barrier cannot be written, the
manager refuses the stop, the service does not stop within 45 seconds, it is
stopped but its control group still holds processes, or the manager cannot be
queried. `RestartPreventExitStatus=2` in the unit keeps the refusal sticky.
`build-limits-status` cannot connect yet; read the unit instead:

```bash
systemctl --user status buildlimits-supervisor.service
journalctl --user --unit buildlimits-supervisor.service
```

Restore manager connectivity if needed, wait for the cache's control group to
empty or stop the surviving processes, leave sccache stopped, then start the
supervisor explicitly. After a refused stop the restart barrier file is left in
place, at `$XDG_RUNTIME_DIR/buildlimits/cache-recovery`, so the cache cannot
return unaccounted; the next accounted start-up retires it.

### Unhealthy supervisor and drain faults

An internal error or pool failure makes the supervisor unhealthy: it refuses
new work and drains, retaining tokens for any work it cannot yet account for.
If a drain query fails repeatedly, the supervisor reports an operator action
after eight consecutive failures and keeps retrying every 30 seconds. Its unit
uses `TimeoutStopSec=infinity` so that systemd cannot kill the token owner
before work is accounted. Once the drain is accounted, the unhealthy supervisor
exits with status 1 and `Restart=always` may start a fresh instance through the
same start-up checks.

Cancellation or signal termination of a cached build can also leave a token in
the `SERVER_DRAIN` state while the supervisor stays healthy. That slot stays
held until the cache service is verified stopped and empty; enough retained
slots can pause new admission. The recovery transaction below is the supported
way out.

## Restart the supervisor after an upgrade

After installing new supervisor code, one command starts it without failing a
queued build, killing a running compiler group, or leaving the host needing
operator action:

```bash
uv tool install --python 3.14 --reinstall git+https://github.com/leynos/buildlimits
systemctl --user restart buildlimits-supervisor.service
```

There is no need to wait for an idle reading first. The supervisor unit must
carry `KillMode=mixed`, described under
[Units the restart relies on](#units-the-restart-relies-on). A supervisor older
than the 2026-10-19 amendment to ADR 001 loses a build when a restart lands
while that build is being placed in its scope and the unit lacks the directive;
the adapter retries the request, but `KillMode=mixed` remains required because
it also keeps the stop away from the supervisor's other helper commands. The
restart works as follows.

1. `systemctl` sends `SIGTERM`. The supervisor stops launching and requesting
   tokens but terminates nothing: every running compiler group runs to its own
   end, and its token returns to the pool when its scope is empty.
2. Builds that are waiting, and builds that arrive meanwhile, stay queued. They
   see only a longer wait.
3. When the last group has finished, the supervisor tells every waiting build
   to try again and exits 0. Each adapter reconnects, up to the five minutes it
   allows for an unreachable supervisor, and is queued by the new code. A build
   already admitted is never retried; it finishes first.
4. `systemd` starts the new code. It terminates any surviving scope, stops the
   managed cache if it is still running, proves it stopped and empty, and only
   then opens the pool and reports `READY=1`. The cache is started again behind
   that notification, so it is back within seconds.

`systemctl --user restart` does not return until the new supervisor is ready,
which can be as long as the longest running compile. While it waits, from
another terminal:

```plaintext
$ systemctl --user status buildlimits-supervisor.service
● buildlimits-supervisor.service - Compile admission supervisor
     Active: deactivating (stop-sigterm) since Sun 2026-10-18 21:33:18 CEST; 2s ago
   Main PID: 3338429 (build-limits-su)
     Status: "Stopping: waiting for running compiler groups to finish"
$ build-limits-status
capacity 4, held 2, healthy, restarting once running groups finish, caching through sccache
no requests waiting
g1 running (cached): cargo check in /home/owner/project, 2.3s
g2 running (cached): cargo check in /home/owner/other, 41.0s
```

Waiting requests appear under `waiting` as usual. When the restart is done,
`systemctl --user restart` exits 0 and the unit reads
`Active: active (running)` with the new `Main PID` and
`Status: "Compiler admission ready; managed cache may start"`. In the journal,
the old instance logs `stopping: finishing running compiler groups before exit`
and the new one logs
`start-up found a live managed cache … stopping it under the restart barrier`
followed by `admitting up to N compiler group(s)`. Until the next cache poll, a
few seconds, groups the new supervisor admits compile without the cache, as
after any cache restart.

If the groups that remain are cached ones that ended by signal, their tokens
return only once the cache is verified stopped and empty, so the stopping
supervisor opens the recovery transaction described below by itself, and the
restart continues when it completes. Only the graceful stop does this; the
immediate stop below refuses the queue and cancels the groups instead.

`systemctl --user stop` performs the same graceful stop, except that nothing
starts the new code: waiting builds keep retrying for the five minutes their
adapters allow for an unreachable supervisor, and then fail.

If waiting is not acceptable, cancel the running groups instead. `SIGINT` is
the immediate stop: the queue is refused as `unhealthy`, every running group is
terminated, and `Restart=always` brings the supervisor back through the restart
barrier.

```bash
systemctl --user kill --signal=SIGINT buildlimits-supervisor.service
```

Two limits apply. The first restart onto a release that contains this behaviour
is performed by the previous release's `SIGTERM` handler, which still cancels
running groups; make it at a quiet point. And a user-manager shutdown also sends
`SIGTERM`: the manager's own timeout ends the wait, guildmaster returns the
pool, and the next start-up terminates any surviving scope.

### Units the restart relies on

The restart relies on these directives in `buildlimits-supervisor.service`. All
but `KillMode=mixed` are the ones the role has always shipped; a role that
changes any of them changes the restart's safety.

```ini
[Service]
KillMode=mixed
Type=notify
NotifyAccess=main
Restart=always
RestartPreventExitStatus=2
TimeoutStopSec=infinity
```

`KillMode=mixed` makes a stop send `SIGTERM` to the supervisor's main process
only. The default, `KillMode=control-group`, sends it to every process in the
unit's control group, including the `busctl` that the supervisor is running at
that moment to place a newly admitted compiler in its transient scope. A
restart that landed in that window killed the `busctl`, and the build was lost
with the journal line `cannot create buildlimits-<pid>-<group>.scope:` and
nothing after the colon. With `mixed`, systemd sends `SIGKILL` to whatever is
left in the control group only after the supervisor has exited, so a stray
process can no longer hold a stop open under `TimeoutStopSec=infinity`. Running
compilers are unaffected either way: they live in their own scopes under the
compilers slice, outside the supervisor's control group.
`buildlimits-sccache.service` keeps the default kill mode, because its stop is
the recovery transaction's stop and must leave its control group empty.

A host provisioned before this directive existed still runs the default. To
check, and after the role has rewritten the unit and
`systemctl --user daemon-reload` has run, read the loaded value:

```bash
systemctl --user show buildlimits-supervisor.service -p KillMode
```

```plaintext
KillMode=mixed
```

Until it reads `mixed`, a restart that lands during a placement is still
survivable, because the supervisor refuses such a request as `restarting` and
its adapter retries it, but a stop also signals the supervisor's other helper
commands (cache probes and stop jobs), which the supervisor then has to retry.
The immediate stop is unaffected by the kill mode: `systemctl kill` signals the
whole control group unless `--kill-whom=main` is given.

`Type=notify` makes `systemctl restart` return when the new supervisor is ready.
`TimeoutStopSec=infinity` stops systemd from killing a supervisor that is
waiting for running groups. `Restart=always` with `RestartPreventExitStatus=2`
brings the supervisor back after a health failure or `SIGINT`, but leaves a
start-up that needs an operator down. The unit must carry no `ExecReload`, and
`buildlimits-sccache.service` must not be bound to the supervisor with
`PartOf=`, `BindsTo=` or `Requires=`, so that restarting the supervisor never
stops or restarts the cache by itself. The cache's restart barrier stays:

```ini
[Unit]
ConditionPathExists=!%t/buildlimits/cache-recovery
```

## Recover a retained managed cache

An interrupted cached build can leave accepted work inside the managed sccache
server after its compiler scope has emptied. No compiler process is visible,
yet the work is live, so admission keeps the token, the queue grows and held
tokens never fall. `build-limits-status` reports the condition:

```plaintext
capacity 4, held 2, healthy, caching through sccache
drain fault kind: live_work
drain check failed; tokens retained: retained detached work: live sccache
service may retain detached requests; visible compiler roots=0; operator
accounting required
fallback recovery action: Automatic recovery of the managed sccache service is
pending and needs no operator action yet. As a fallback only, if it does not
start or complete, run build-limits-recover to stop the service under the
admission barrier, verify it is stopped and empty, and release the retained
tokens; tokens remain held until that verification succeeds.
cache recovery idle: automatic recovery may start in 212.0s
waiting 9.0s: cargo check in /home/owner/project
g1 server-drain (cached): cargo check in /home/owner/project, 88.0s
g2 running (cached): cargo build in /home/owner/other, 30.0s
```

Here a cached compile is still running, which defeats the prompt proof, so only
the retained drain's age can open the recovery.

Each prefix is one rendered line; the fault and guidance lines are wrapped here
to fit the page.

Elapsed time, idle CPU and a missing compiler process are never proof of
completion. Only the supervisor's own accounting releases a token, and it
demands a verified stopped-and-empty service. The recovery transaction performs
that accounting in four phases, at most one per supervisor tick:

1. `quiescing` writes the restart barrier,
   `$XDG_RUNTIME_DIR/buildlimits/cache-recovery`, which the cache unit's
   `ConditionPathExists=!` honours, so the cache cannot return early.
2. `stopping` asks the user manager to stop the cache.
3. `accounting` waits until the drain probe verifies the stopped, empty
   service; that verification returns the tokens.
4. `restarting` removes the barrier, then starts the cache again.

With the default `cache_recovery = "starved"` policy, the supervisor opens a
transaction by itself when a retained drain holds a token while requests wait,
on either of two kinds of evidence:

- The cache holds no visible compiler roots and no cached compile is running,
  so the retained work is provably detached. The supervisor acts at once.
- The drain has been retained without a break for 300 seconds. On a busy host
  every freed slot is refilled with a cached compile at once, so the first
  proof may never be available; without this one, a single retained token would
  cost the host a slot until every slot was retained.

Either way the transaction holds new compiles off the cache while it runs, and
it stops the cache only once no cached compile is running. An automatic stop
also wants the cache to show no visible compiler roots, but it will not wait
for them for ever. Once no cached compile is running, a root still visible in
the cache serves no admitted request, because every request admitted since the
barrier was written compiles without the cache. If such roots are seen without
a break for 600 seconds, for example because a compile inside the cache has
hung, the automatic stop proceeds anyway, writes a journal line beginning
`cache recovery: stopping the managed cache with compiler roots` and adds one
to the `recovery_roots_deadline_stops` count of the `cache_recovery` metrics
event. The 600 seconds restart from nothing whenever a cached compile starts
moving again or a probe cannot count the roots, and each transaction measures
its own. A count the probe could not read is never treated as a visible root.
While the roots delay the stop, `build-limits-status` shows
`cache recovery: visible compiler roots have delayed the stop for ...`. A manual
`build-limits-recover` never waits for roots, and a faulted user manager still
refuses every stop.

After each transaction the automatic trigger rests before it may act again: 60
seconds after a clean recovery, and 900 seconds after one whose restart was
still refused, so that a cache slow to come back cannot exhaust a second set of
attempts. While a retained drain waits behind that rest, or behind the stale
proof, `build-limits-status` says so:

```plaintext
cache recovery idle: automatic recovery may start in 42.5s
```

Tokens retained beside that line are waiting for the trigger, not stuck. The
line counts down to the instant the trigger can open: while a drain is retained
with requests waiting, it shows the later of the cooldown and the time left to
the stale proof (300 seconds less the drain's age). It appears only while
waiting will open the trigger. A drain that clears, a queue that empties, a
`manual` policy, a faulted user manager or a non-`sccache` cache mode removes
it, and so does a `starved` policy with nothing retained, even during the
cooldown; the `cooling_for` field still reports the rest. A transaction that
ended with its restart refused also keeps its `cache recovery last fault:`
line, during the rest and after it, until the next transaction succeeds. With
`manual`, the supervisor waits for an operator:

```bash
build-limits-recover --wait --wait-timeout 300
```

The command opens the transaction even while compiler groups that use the
managed cache are running or draining, but the stop waits for them, because
stopping the cache would interrupt them; until then the transaction reports
`cache recovery stopping` with the reason as its last fault. Groups composed
without the cache do not hold the stop, and every request admitted while a
transaction runs is composed that way, so a busy queue cannot hold it off. The
command refuses outright only while the user manager's transport is faulted,
because a service whose state cannot be read must not be stopped. A supervisor
in `direct` cache mode manages no cache and answers with `no-cache-recovery`.

`--wait` polls the status document every 2 seconds for up to `--wait-timeout`
seconds (300 by default). A failed step is retried after 30 seconds, then 60,
and so on in 30-second steps up to 120 seconds apart; the eighth consecutive
failure logs guidance for the phase. None of these numbers is a deadline for
the accounting (the roots grace above only decides when an automatic stop may
be issued, and the verified stopped-and-empty service still releases every
token): a refused stop is retried for as long as the fault persists, and
accounting waits for as long as the drain probe needs. Only the restart gives
up: after eight failed attempts the transaction goes idle with the tokens
already released and the last fault reported.

| Exit status | Meaning                                                                        |
| ----------- | ------------------------------------------------------------------------------ |
| `0`         | The transaction finished and no live cache work retains a token                |
| `1`         | The transaction is still running, was blocked, timed out or ended with a fault |
| `2`         | The supervisor could not be asked, so the transaction's state is unknown       |

_Table 5: `build-limits-recover` exit statuses._

Verify the outcome from the supervisor, not from elapsed time:

The `held`, `drain_fault`, `drain_fault_kind` and `caching` fields below are
cache-recovery diagnostics, not generic admission readiness. For provisioning
or general monitoring, use `build-limits-status --ready` or check
`.admission.ready` in the JSON document. A successful status query or `healthy`
alone reports process and pool health, not usable admission. Keep health-only
checks where process health is the intended signal.

```bash
build-limits-status --json | jq '{held, drain_fault, drain_fault_kind, caching}'
```

### What to do when status shows a retained drain

Read the `cache recovery` line and the label of the action line first.

- `cache recovery idle: automatic recovery may start in Ns` with a
  `fallback recovery action:` line means the supervisor will open the recovery
  itself in at most `N` seconds. Keep waiting; do not escalate and do not run
  `build-limits-recover` yet. Escalate only if the line disappears with the
  drain still retained and requests still waiting, or the countdown reaches
  zero without a `cache recovery quiescing` line appearing.
- `cache recovery quiescing`, `stopping`, `accounting` or `restarting` is the
  recovery under way. Keep waiting while its attempts stay below eight.
- `required recovery action:` is a real instruction. It appears when waiting
  cannot help: the `manual` policy, a faulted user manager, a stopped cache
  that is not empty, or a transaction that used its eight attempts. A refused
  restart keeps its `cache recovery last fault:` line.

The `--json` document backs the lines. Under `cache_recovery`, `automatic` says
whether the cache mode and policy let the trigger act, `opens_in` is the
seconds the idle trigger still waits on time alone (`null` when waiting will
not open it, or a transaction is running), and `pending` says a recovery is due
or under way without an operator. At the top level
`recovery_action_is_fallback` is true when the text of `recovery_action` calls
the command a fallback. The journal says
`drain recovery is pending automatically; operator action is only a fallback`
in that case, and `drain requires operator action` otherwise.

Operators upgrading from a supervisor that printed
`required recovery action: Run build-limits-recover …` for every live-work
fault should expect the label and text above for the automatic policy. The
countdown line itself is unchanged, so instructions that tell agents to keep
waiting on it need no edit; instructions that escalate on a live-work fault
with no `cache recovery` line are now rarely triggered, since that picture only
remains for faults that waiting will not cure.

Success is `drain_fault: null` and `drain_fault_kind: null`. A waiting request
can take a released token at once, so `held` may settle above zero; that is the
queue moving again. `caching: false` only says the last reachability probe
found no server; a cache that stays down costs cache hits, not correctness.

Read the fault kind before acting, because stopping the cache is the right
answer to only one of them:

| `drain_fault_kind`  | Meaning                                                     | Action                                                                                                                                                                                |
| ------------------- | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `live_work`         | A live cache may retain detached work                       | Under `starved`, wait while a countdown or `fallback recovery action:` shows; otherwise run `build-limits-recover`, which stops the cache and proves it empty before releasing tokens |
| `stopped_populated` | The cache is stopped but its control group still holds work | Inspect the surviving processes and wait for them to exit; tokens stay held until then                                                                                                |
| `manager`           | The cache's state could not be read at all                  | Restore the systemd user-manager transport. Do not stop or restart the cache                                                                                                          |

_Table 6: Drain fault kinds and their remedies._

Stopping sccache by hand releases no token, and neither does restarting the
supervisor; only the accounting does. While a transaction runs, a manual
`systemctl --user restart buildlimits-sccache.service` cannot bring the cache
back early, because the unit skips the start while the barrier exists. If the
barrier itself cannot be removed, the transaction stays in its restart phase
until its attempts run out; restore the runtime directory and run
`build-limits-recover` again rather than starting the cache by hand.

A supervisor that dies mid-transaction leaves the barrier behind. Its next
start-up attempts the same accounting: success clears the barrier and starts
the cache, while failure keeps the barrier and refuses to open the pool.

## Metrics

The supervisor and adapters write bounded metric records without request
identifiers to the native journal. `build-limits-metrics` reads them back for a
time window:

```bash
build-limits-metrics --since 1h --max-records 10000 --source all
build-limits-metrics --since 1d --source supervisor --json
journalctl --user BUILD_LIMITS_SCHEMA=1 -o json
```

| Option          | Default | Meaning                                                    |
| --------------- | ------- | ---------------------------------------------------------- |
| `--since`       | `1h`    | The window, as a journalctl relative time such as `1d`     |
| `--max-records` | `10000` | The record cap, from 1 to 100,000; reaching it is reported |
| `--source`      | `all`   | `all`, `supervisor` or `adapter`                           |
| `--json`        | off     | Emit the report, with bounded event history, as JSON       |

_Table 7: `build-limits-metrics` options._

Each query has a 30-second deadline and a 64 MiB output limit; exceeding either
is a query failure, never a partial report. The command exits 0 with a report,
1 when the journal cannot be read, and 2 for invalid options. An empty readable
window succeeds with a warning.

Reports include terminal request rates, outcome and reason counts, nearest-rank
p50, p90, p99 and maximum wait and runtime, queue-depth and held token
summaries, cache-path counts and the direct-fallback ratio. They also summarize
reachability and health transitions, pool failures, supervisor starts, and
adapter outcomes and connection attempts.

The supervisor also writes OpenTelemetry `scope_placement` spans to the native
journal. An adapter writes `adapter_restart_retry` only when it has to retry
because the supervisor is restarting or unavailable. Query spans separately with
`journalctl --user BUILD_LIMITS_TRACE_EVENT=span_completed -o json`; the
metrics command ignores these records. Span fields describe the operation,
elapsed time, bounded outcome and failure category, known stop mode and retry
attempt. They do not include compiler arguments, paths or raw errors.

When the window holds pressure observations, the report adds a `pressure`
section: the latest descriptor retention and budget, lifetime descriptor and
queue high-water values, listener pauses, drain progress and adapter busy
retries. High-water values are lifetime maxima of the producer, not maxima for
the window. Drain attempt, failure and recovery counters are cumulative
snapshots, so the report shows the largest value observed rather than a sum.

Supervisor records are trusted only when journald supplies
`_SYSTEMD_USER_UNIT=buildlimits-supervisor.service`; adapter records are
self-reported. Journal rate limits and retention can lose history, and the raw
`journalctl` query above applies none of the command's trust filtering.

## Change the capacity or disable admission

Change capacity only at a quiet point. Confirm with `build-limits-status` that
no tokens are held, no requests wait and no groups run, and keep builds
quiescent throughout. Stop the managed cache first, then the supervisor;
replace the pool only after no process holds the old device; then start the
supervisor before the cache. The supervisor's readiness notification orders the
cache start after its start-up accounting.

To disable admission, restore Cargo's previous `rustc-wrapper` and `rustdoc`
settings and restart the launchers that hold the old environment. Leave the
supervisor running until those processes have gone, so that no compiler still
pointing at the adapters is stranded.
