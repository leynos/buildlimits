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
- Python 3.14 or newer. The package has no runtime dependencies beyond the
  standard library.
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

| Key                     | Default                                        | Meaning                                                                                          |
| ----------------------- | ---------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `device`                | `/dev/guild`                                   | The guildmaster token device                                                                     |
| `slots`                 | `2`                                            | The pool's configured capacity, reported by status                                               |
| `socket_path`           | `$XDG_RUNTIME_DIR/buildlimits/supervisor.sock` | Where the supervisor listens                                                                     |
| `lock_path`             | `$XDG_RUNTIME_DIR/buildlimits/supervisor.lock` | The singleton lock                                                                               |
| `compilers_slice`       | `buildlimits-compilers.slice`                  | The slice that holds every compiler scope; must end in `.slice`                                  |
| `cancel_grace_seconds`  | `10.0`                                         | Seconds between `SIGTERM` and `SIGKILL` for a terminated group                                   |
| `drain_timeout_seconds` | `120.0`                                        | The longest start-up waits for surviving groups to drain                                         |
| `cache_mode`            | `sccache`                                      | `sccache` composes admitted compilers with the managed cache; `direct`                           |
| `sccache_executable`    | `sccache`                                      | The sccache client used in `sccache` mode                                                        |
| `sccache_unit`          | `buildlimits-sccache.service`                  | The managed cache's user unit                                                                    |
| `sccache_socket`        | `$XDG_RUNTIME_DIR/buildlimits/sccache.sock`    | The managed cache's socket                                                                       |
| `cache_recovery`        | `starved`                                      | `starved` or `manual`; see [Recover a retained managed cache](#recover-a-retained-managed-cache) |
| `recovery_marker`       | `$XDG_RUNTIME_DIR/buildlimits/cache-recovery`  | The restart barrier the managed cache unit honours                                               |

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

## Status

```bash
build-limits-status
build-limits-status --json
journalctl --user --unit buildlimits-supervisor.service
```

`build-limits-status` shows the capacity, held tokens, waiting requests and
running compiler groups. Guildmaster reports none of these, so the status comes
from the supervisor. The plain-text output also explains an unhealthy state,
the current drain fault and any required recovery action. The `--json` document
adds descriptor limits, the open-descriptor sample, request-owned retention,
the high-water mark and the budget. These are point-in-time diagnostics, not
history; `build-limits-metrics` summarizes history.

| Exit status | Meaning                                                     |
| ----------- | ----------------------------------------------------------- |
| `0`         | The supervisor answered and is healthy                      |
| `1`         | The supervisor answered but is unhealthy, or did not answer |

_Table 4: `build-limits-status` exit statuses._

Under `descriptors`, `open` is the sampled count of open descriptors, or `null`
with a numeric `sampling_errno` when enumeration failed; the text summary says
`unavailable`. Limits of `-1` mean unlimited. These gauges help track headroom;
they do not prove that descriptors are leak-free.

### Start-up refusal

Before it opens the pool, the supervisor requires the managed sccache service
to be stopped and its control group empty, because a live server with no
visible compiler children may still hold accepted work waiting for a Cargo
jobserver token. If that cannot be verified, start-up exits with status 2
before the status socket opens, and `RestartPreventExitStatus=2` in the unit
keeps the refusal sticky. `build-limits-status` cannot connect yet; read the
unit instead:

```bash
systemctl --user status buildlimits-supervisor.service
journalctl --user --unit buildlimits-supervisor.service
```

Restore manager connectivity if needed, coordinate a quiet stop of the cache,
verify the stopped service and empty control group, leave sccache stopped, then
start the supervisor explicitly. Do not interrupt unrelated builds.

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

## Recover a retained managed cache

An interrupted cached build can leave accepted work inside the managed sccache
server after its compiler scope has emptied. No compiler process is visible,
yet the work is live, so admission keeps the token, the queue grows and held
tokens never fall. `build-limits-status` reports the condition:

```plaintext
capacity 2, held 2, UNHEALTHY, caching through sccache
drain fault kind: live_work
drain check failed; tokens retained: retained detached work: live sccache
service may retain detached requests; visible compiler roots=0; operator
accounting required
required recovery action: Run build-limits-recover to stop the managed sccache
service under the admission barrier, verify it is stopped and empty, and
release the retained tokens; tokens remain held until that verification
succeeds.
waiting 9.0s: cargo check in /home/owner/project
g1 server-drain (cached): cargo check in /home/owner/project, 42.0s
```

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
transaction by itself when a retained drain is starving waiting work and the
cache holds no visible compiler roots. With `manual`, it waits for an operator:

```bash
build-limits-recover --wait --wait-timeout 300
```

The command refuses to start under a running or draining compiler group that
uses the managed cache, because stopping the cache would interrupt it. Groups
composed without the cache do not block it; every request admitted while a
transaction runs is composed that way, so a busy queue cannot hold the stop
off. It also refuses while the user manager's transport is faulted, because a
service whose state cannot be read must not be stopped. A supervisor in
`direct` cache mode manages no cache and answers with `no-cache-recovery`.

`--wait` polls the status document every 2 seconds for up to `--wait-timeout`
seconds (300 by default). A failed step is retried after 30 seconds, then 60,
and so on in 30-second steps up to 120 seconds apart; the eighth consecutive
failure logs guidance for the phase. None of these numbers is a deadline for
the accounting: a refused stop is retried for as long as the fault persists,
and accounting waits for as long as the drain probe needs. Only the restart
gives up: after eight failed attempts the transaction goes idle with the tokens
already released and the last fault reported.

| Exit status | Meaning                                                                        |
| ----------- | ------------------------------------------------------------------------------ |
| `0`         | The transaction finished and no live cache work retains a token                |
| `1`         | The transaction is still running, was blocked, timed out or ended with a fault |
| `2`         | The supervisor could not be asked, so the transaction's state is unknown       |

_Table 5: `build-limits-recover` exit statuses._

Verify the outcome from the supervisor, not from elapsed time:

```bash
build-limits-status --json | jq '{held, drain_fault, drain_fault_kind, caching}'
```

Success is `drain_fault: null` and `drain_fault_kind: null`. A waiting request
can take a released token at once, so `held` may settle above zero; that is the
queue moving again. `caching: false` only says the last reachability probe
found no server; a cache that stays down costs cache hits, not correctness.

Read the fault kind before acting, because stopping the cache is the right
answer to only one of them:

| `drain_fault_kind`  | Meaning                                                     | Action                                                                                     |
| ------------------- | ----------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `live_work`         | A live cache may retain detached work                       | Run `build-limits-recover`; it stops the cache and proves it empty before releasing tokens |
| `stopped_populated` | The cache is stopped but its control group still holds work | Inspect the surviving processes and wait for them to exit; tokens stay held until then     |
| `manager`           | The cache's state could not be read at all                  | Restore the systemd user-manager transport. Do not stop or restart the cache               |

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

The supervisor and the adapters write bounded, identifier-free records to the
native journal. `build-limits-metrics` reads them back for a time window:

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
