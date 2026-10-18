# Architectural decision record (ADR) 001: graceful supervisor restart

## Status

Accepted. A plain `systemctl --user restart buildlimits-supervisor.service`
becomes the non-disruptive restart. `SIGTERM` drains instead of cancelling, and
start-up stops a live managed cache itself instead of refusing it. No unit-file
change is needed.

## Date

2026-10-18.

## Context and problem statement

An operator installs new supervisor code with `uv tool install` and must then
run the new code without failing queued builds, killing running compiler
groups, or leaving the host needing operator action. On 2026-10-18, on host
`rohga`, an installer waited for an idle status reading, ran `uv tool install`,
and ran `systemctl --user restart buildlimits-supervisor.service` about a
second later. Builds had started again in that second. The restart failed in
two separate ways.

1. The stopping supervisor logged
   `stopping: draining every compiler group before exit`. `Supervisor.stop`
   called `Admission.health_failed`, which refuses the whole queue as
   `unhealthy` and terminates every running group. The adapters of the refused
   requests failed their builds, and the terminated groups were cancelled
   builds.
2. The new supervisor logged
   `restart barrier passed; terminated 0 surviving scope(s)` and then exited 2
   with `startup cannot account for managed cache work`, giving as the cause
   `live sccache service may retain detached requests; visible compiler roots=0`.
   `RestartPreventExitStatus=2` kept it down, so every adapter got "connection
   refused" until an operator stopped the idle cache by hand.

### Why the two shutdowns differed

The supervisor never stops the managed cache on exit. The cache is a separate
unit with its own `Restart=always`; the supervisor unit only `Wants=` it and
orders itself `Before=` it. Start-up demands `ActiveState` inactive with an
empty control group (`ManagedSccache.accounted_drain`), so a restart is clean
only if something stopped the cache before the new supervisor looked at it. The
journal of the host shows the two cases:

- At 19:47:58 a manual recovery transaction had written the restart barrier
  and stopped the cache at 19:47:32. The old supervisor was stopped while the
  cache was stopped, so the new one found it stopped and empty, retired the
  barrier (`reclaimed an interrupted recovery`) and started the cache. That
  restart was clean by accident of timing.
- At 20:48:05 a starved recovery had finished at 20:39:50 and restarted the
  cache. The cache was live when the old supervisor exited 15 ms after
  `SIGTERM`, so the new supervisor met a live server and refused it.

Any restart with a live cache therefore ends in exit 2. The incident is not
caused by cache recovery; it is that no restart path stops the cache.

## Decision drivers

- Never kill an admitted compile, and never release a token without the
  verified stopped-and-empty accounting.
- The barrier ordering stays: write the barrier, stop, account, clear, start.
- Guildmaster returns a process's tokens when its last handle closes, so a
  successor cannot adopt a predecessor's running groups: each running group
  holds a token and a requester connection that belong to the old process.
- The installed supervisor unit is managed by Ansible, and a procedure that
  needs a role change cannot be used until the role is deployed.
- The installer's habitual command is `systemctl restart`.

## Options considered

### Option A: a new verb (`systemctl reload` through `ExecReload`)

The supervisor would stop admitting on a signal such as `SIGUSR1` and exit when
drained. It needs `ExecReload` in the unit, so it needs an Ansible change. It
returns as soon as the signal is sent, so the operator learns nothing about
completion, and `restart` stays dangerous. Rejected.

### Option B: hand over the listener and the pool (socket activation, fd store)

Systemd could keep the listening socket and the pool handle across the restart.
The listener alone buys nothing that the adapter's reconnection does not give,
because connecting during a restart only waits. Handing over the pool handle
would keep the tokens alive, but the running groups' requester connections
cannot be handed over, and the new process would have to rebuild each group's
state from outside. That is a persistent-format and trust-boundary change for
no operator benefit. Rejected.

### Option C: make plain `systemctl restart` non-disruptive

`SIGTERM` is what systemd sends for `stop` and `restart`. The supervisor
already runs with `TimeoutStopSec=infinity`, so a stop that waits for running
groups is already permitted. Chosen.

| Topic                 | A: `reload` | B: hand-over | C: drain on `SIGTERM` |
| --------------------- | ----------- | ------------ | --------------------- |
| Unit change needed    | yes         | yes          | no                    |
| Habitual command safe | no          | yes          | yes                   |
| Running groups kept   | yes         | adopted      | yes, until they end   |
| Completion visible    | no          | yes          | yes (`restart` waits) |
| New persistent format | no          | yes          | no                    |

_Table 1: Trade-offs between the three restart mechanisms._

## Decision outcome

Option C. The operator command is
`systemctl --user restart buildlimits-supervisor.service`. It blocks until the
old supervisor has finished its running groups and the new one has reported
`READY=1`.

- `SIGTERM` starts a graceful stop. `Admission.begin_closing` puts admission in
  a closing mode that requests no token and launches nothing but terminates
  nothing. Running groups finish and return their tokens. Requests that wait or
  arrive stay queued, so adapters see only a longer wait. When the last group
  has emptied, whatever still waits is refused as `restarting` and the
  supervisor exits 0. A request that arrives once no group is left is refused
  at once. `systemctl status` shows the line
  `Stopping: waiting for running compiler groups to finish`, and
  `build-limits-status` says `restarting once running groups finish`.
- A closing supervisor that has only `SERVER_DRAIN` groups left opens the
  recovery transaction itself, because those tokens return only when the cache
  is verified stopped and empty and nothing else would stop the cache when no
  request waits.
- Before it exits, the supervisor hands back any token its pool reader took for
  a request nobody will launch. Guildmaster would return it when the handle
  closes; a pool that does not account for its readers would lose it.
- Adapters treat a `restarting` refusal, and a connection that closes or resets
  before `Admitted`, as nothing having run: they reconnect. A wait spent queued
  does not count against the five-minute window in which a supervisor may be
  unavailable. A connection lost after `Admitted` still fails the build,
  because the compiler may have run.
- Start-up stops a live managed cache itself. By then it holds the singleton
  lock and has terminated every scope under the compilers slice, so no admitted
  group can use the cache. It writes the restart barrier, queues the stop, and
  waits for the unchanged strict accounting check to pass; the existing
  reclamation then retires the barrier and starts the cache. Failure to write
  the barrier, to stop the cache, or to prove it stopped and empty within 60
  seconds still exits 2, as do a stopped cache that holds processes and a user
  manager that cannot be read.
- `SIGINT` keeps the former behaviour as the explicit escape hatch: the queue
  is refused and every running group is terminated.

## Known risks and limitations

- The first restart onto a release with this change is performed by the
  previous release's `SIGTERM` handler, which is still disruptive. Only the
  start-up half protects that restart, so it should be made at a quiet point.
- A restart waits for the longest running compile. `SIGINT` escalates.
- Requests that were queued when the stop began rejoin the new supervisor's
  queue in the order their adapters reconnect, not their old order.
- A user-manager shutdown also sends `SIGTERM`, and the manager's own timeout
  ends the wait; the token pool is then returned by guildmaster, and the next
  start-up terminates any surviving scope.
- Until the next cache poll, groups the new supervisor admits compile without
  the cache, as after any other cache restart.

## Architectural rationale

The change keeps the barrier ordering and the single authority for releasing a
token. It adds no state that outlives a process and no trust boundary, and it
moves the cache stop to the one place that already knows no admitted group can
use the cache.
