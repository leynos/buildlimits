# Keep a graceful restart from breaking a scope placement

This ExecPlan is a living document. Status: IN REVIEW.

## Purpose

On host `rohga` (2026-10-19, installed `de73710`) a graceful
`systemctl --user restart buildlimits-supervisor.service` under load logged:

```plaintext
Stopping buildlimits-supervisor.service - Compile admission supervisor...
build-limits-supervisor: group g141702: cannot create buildlimits-3952476-g141702.scope:
build-limits-supervisor: stopping: finishing running compiler groups before exit
```

The text after `cannot create ...scope:` is empty, and the request's build
failed instead of being retried. ADR 001 promises that a restart cancels
nothing and fails no build, so this is a gap in that promise. After this change
the restart is non-disruptive at the one point where it was not: a stop that
lands while the supervisor places a freshly launched compiler in its transient
scope neither breaks the placement nor loses the request.

## Hypothesis and verdict

Hypothesis (from the supervising session): the unit has the default
`KillMode=control-group`, so a stop sends `SIGTERM` to every process in the
supervisor's control group, including the `busctl` that
`UserManager.start_scope` runs and the held launch child; `Groups.place` then
fails the placement and the request is lost rather than refused as `restarting`.

Verdict: confirmed for `busctl`, refined for the held child. Evidence, all from
this repository's real-systemd harness on this host:

- `systemctl --user show buildlimits-supervisor.service -p KillMode` reads
  `control-group` (read only), as do the fixture units.
- The red scenario (`tests/test_restart_during_placement_systemd.py`) holds the
  placement open with a `busctl` shim, restarts the unit, and reproduces the
  incident journal line for line: `Stopping` then
  `group g1: cannot create buildlimits-<pid>-g1.scope:` with empty stderr, then
  `stopping: finishing running compiler groups before exit`. The log order
  itself is evidence: the `SIGTERM` handler only records a flag, so the stop is
  honoured on the first tick after the failed placement, which is why
  `cannot create` precedes `stopping`.
- The shim records receiving `SIGTERM`. The adapter reports
  `the supervisor refused: launch-failed` and exits 1 (a lost build). With a
  child diagnostic the same path would report `exec-failed`; here none exists.
- The held launch child is not a casualty. The shim's census of the control
  group taken 0.3 s after the signal shows the supervisor and the held child
  both alive (state `S`). The forked child inherits the supervisor's Python
  `SIGTERM` handler, which only sets a flag in the child, and it does not reset
  signals until after the barrier. Only the `busctl` dies.
- Experiment (isolated transient units, since removed): with
  `TimeoutStopSec=infinity`, a stop of a service whose main process exits on
  `SIGTERM` but whose orphan ignores it completes in 0.03 s under
  `KillMode=mixed` (the orphan is `SIGKILL`led as soon as the main process is
  gone) and hangs indefinitely under `KillMode=control-group`.

## Constraints

- Do not stop, restart, signal or reconfigure the live buildlimits services, and
  do not install on the host. Real-systemd tests use their own isolated units.
- Do not edit `dev-env-rocky`; the role change is a separate piece of work and
  this plan only specifies the directives it must render.
- Do not change recovery timing, the barrier ordering, or the adapter's retry
  window.
- Each logical change is its own commit, in en-GB-oxendict.

## Design

Fix 1, the unit contract. The supervisor unit gains `KillMode=mixed`. A stop
then sends `SIGTERM` to the main process only, so the `busctl`, `systemctl` and
`busctl` fallback processes the supervisor runs synchronously are not
signalled. Once the main process has exited, systemd sends `SIGKILL` to
whatever remains in the control group, whatever `TimeoutStopSec` says (verified
above), so `TimeoutStopSec=infinity` still never kills a draining supervisor
and can no longer be held open by a stray process. Running compilers live in
transient scopes under the compilers slice, outside the supervisor's control
group, so the kill mode never touches them. A held launch child is in the
supervisor's group only until it is placed; if the supervisor ever exits with
one left, it has run nothing (its barrier closed, so it exits 126), and
`SIGKILL` is right.

The cache unit is deliberately not changed. A supervisor restart never stops
the cache (the contract test pins that it is never `PartOf`, `BindsTo` or
`Requires`d), so the cache's kill mode is not on the incident's path. The
cache's own stop is the recovery transaction's stop, and its purpose is a
verified stopped-and-empty control group (`StoppedPopulatedCacheError`
otherwise). `control-group` signals the server and its detached compile
children together, which is the semantics that proof wants; `mixed` would only
delay the children's termination to the final `SIGKILL`.

Fix 2, defence in depth. If a scope placement fails while a graceful stop is in
progress, the request is refused as `restarting`, which the adapter retries
within its deadline (`adapter_invocation.py`), instead of `launch-failed`. The
stop is "in progress" when admission is closing or a graceful stop has been
requested but not yet honoured, because the signal handler only records intent
and the failed placement is observed before the next tick honours it (the
incident's own log order). `Supervisor._launch` therefore calls
`supervisor_shutdown.fail_placement`, which honours pending stop requests
before accounting for the failed launch. If an immediate stop is also pending,
it takes effect after the graceful request but before `Admission.launch_failed`
can emit `Shutdown`; the queue is refused as `unhealthy`, so no adapter retries
the failed placement. Otherwise `Admission.launch_failed` refuses as
`restarting` exactly when it is `winding_down`. The log line that names the
real cause is kept; `entry.exec_error` is cleared so that `Requesters.refuse`
does not rewrite the graceful retry reason to `exec-failed`. With no stop in
progress the behaviour is unchanged. The token returns through the existing
`ReturnToken`, the held child is already abandoned by `Groups.place`, and the
request's descriptors close when the refusal forgets it.

Alternatives rejected for Fix 2: retrying the placement in process. The held
child may be unusable, a partially created scope of the same name would make a
second `StartTransientUnit` fail, the retry would hold a token and delay the
drain, and the adapter already has a proven, bounded retry that reaches the new
supervisor. Failing as `launch-failed` only when no stop is in progress keeps
genuine launch failures visible.

Limit: only placement failure (before `Admitted`) is converted. A failed
release after `Admitted` keeps its behaviour, because the adapter treats
anything after `Admitted` as possibly run.

## Helper subprocess inventory

Under `KillMode=control-group`, one `SIGTERM` reaches every process alive in
the control group at that instant. Each helper is synchronous and bounded, so
only one in flight at the stop is affected. After Fix 1 none is signalled.

1. `busctl ... StartTransientUnit` (`UserManager.start_scope`, `Groups.place`):
   the incident. Now unsignalled; if it fails during a stop anyway, Fix 2
   refuses the request as `restarting`.
2. `systemctl --user show` (`UserManager.show`, cache probes and accounting):
   a killed call raises `SystemdError`, and `show` falls back to the `busctl`
   property query; a failure of both is a transient `SccacheError`/fault
   classified `manager`, retried on the next poll, and tokens stay held. Now
   unsignalled.
3. `busctl` property and unit-control fallbacks (`bus_properties`): the same
   fallbacks, same retry. Now unsignalled.
4. `systemctl --user --no-block stop|start` (`UserManager.control_unit`, the
   cache stop under the recovery transaction): falls back to `busctl`; a
   failure of both is retried by `_try_stop` without bound, and by `_try_start`
   up to the failure limit. Now unsignalled.
5. Held launch child (`launch.spawn_held`): survives `SIGTERM` (inherited
   handler); abandoned with `SIGKILL` by `Groups.place` on any failure.
6. Cgroup reads, `cgroup.kill` writes and `os.kill` of group members
   (`cgroups`, `group_drain`): in-process system calls, not subprocesses.
7. `journalctl` (`metrics_transport`) and `rustdoc` probes run in the status and
   adapter processes, outside the supervisor's control group.
8. `ExecStartPre=rm -f` of the cache unit runs in the cache's group at start.
9. `systemctl kill --signal=SIGINT` (the documented immediate stop) signals the
   whole control group whatever the kill mode, because `--kill-whom` defaults to
   `all`. An immediate stop refuses the queue and cancels groups anyway, so a
   placement lost to it is not a gap; the guide notes it and `--kill-whom=main`
   is left to the operator.

## Milestones

1. Plan with the hypothesis evidence committed.
2. Tests-only refactor: extract the restart scenarios' shared world from
   `tests/test_graceful_restart_systemd.py` so that a second module can use it.
3. Red: the `busctl` shim, the harness seams, and two real-systemd scenarios,
   committed as strict expected failures with the red output recorded below.
4. Fix 1: `KillMode=mixed` in the fixture unit, contract test, users' guide,
   ADR 001 amendment and upgrade note. The first scenario goes green.
5. Fix 2: supervisor and admission change with unit and property tests. The
   second scenario goes green.
6. Gates through scrutineer on each candidate and the final head, PR, review
   loop to a mergeable state.

## Verification plan

Invariant I1: the supervisor unit contract carries `KillMode=mixed`, in the
guide's documented block, in the fixture unit, and nowhere contradicted.
Method: the existing three-way contract test
(`tests/test_unit_file_contract.py`) extended with the directive. It fails if
either side drops it. Non-vacuity: the guide block is parsed and must contain
the key.

Invariant I2: under `mixed`, a stop landing mid-placement signals nothing but
the main process and the build completes with its own status, once. Method: the
real-systemd scenario with the `busctl` shim. It fails on the old fixture unit
(red run below). The shim's census is the negative control.

Invariant I3: in sampled healthy, uncached `Admission` histories, a failed
placement is refused as `restarting` after graceful closing and otherwise as
`launch-failed`. The property checks the failed request's exact refusal, its
own token's exact return, and that unrelated groups remain unchanged. The first
close transition and later transitions launch or request no token; only a
leader exit or a requester cancellation can terminate a group. On drain, each
queued request is refused once before one `Shutdown`, with no queue entry left.
Repeated closing is a no-op. A simultaneous immediate stop is handled before a
graceful retry can be emitted by separate supervisor unit tests.

Method: `test_graceful_close_preserves_admission_invariants` in
`tests/test_admission_closing_properties.py` samples up to 40 operations per
history and runs 200 examples with Hypothesis 6.170.1. Its operations are
submit, token, leader exit, group empty, close, queued-request cancellation,
running-group requester cancellation and placement failure. Named examples
exercise both failure classifications, repeated close, multiple groups with
queued requests, early group emptiness, and requester cancellation during
closing. The expectation for graceful closing is tracked independently from
`Admission.closing`.

This is bounded admission-state testing. It keeps admission healthy and
uncached, treats `fail` as a placement failure before a compiler starts, and
does not model cache-server drain, health failure, stop-signal flags, child
execution, or systemd process effects. The real-systemd scenarios cover the
systemd boundary; supervisor unit tests cover simultaneous stop requests. The
finite property does not establish unbounded liveness or formal proof. A
mutation that forces failed placements to `launch-failed` must fail the
graceful witness. Mutation control on commit `db02b38` temporarily changed
`Admission.launch_failed` to rewrite every `Refuse` reason to `launch-failed`,
then ran the property below. The expected outcome is pytest exit 1 at the named
graceful witness, asserting the exact `restarting` refusal; the mutation was
rejected there. Log:
`/tmp/mutation-control-buildlimits-fix-restart-kill-mode-review-feedback-final.out`.
The command was `uv run python - <<'PY'` with this mutation harness; pytest's
exit 1 is the expected outcome.

```python
import pytest
import buildlimits.admission as admission_module
import buildlimits.effects as effects

original = admission_module.Admission.launch_failed

def always_report_launch_failed(self, group_id):
    returned = original(self, group_id)
    return [
        effects.Refuse(effect.request_id, "launch-failed")
        if isinstance(effect, effects.Refuse)
        else effect
        for effect in returned
    ]

admission_module.Admission.launch_failed = always_report_launch_failed
raise SystemExit(pytest.main([
    "-q",
    "tests/test_admission_closing_properties.py::test_graceful_close_preserves_admission_invariants",
]))
```

Invariant I4: with the legacy kill mode, the second real-systemd scenario shows
the busctl was signalled and the build still completes once after a retry.

Axioms: systemd's documented `KillMode=mixed` semantics (verified by the
experiment above); Python signal handlers survive `fork`.

## Progress

- [x] Worktree `fix/restart-kill-mode` from `origin/main` at `cfa70a7`.
- [x] Hypothesis tested with a real-systemd reproduction.
- [x] ExecPlan written.
- [x] Refactor commit (shared restart world).
- [x] Red commit: both scenarios strict `xfail` on the unfixed fixture unit.
  Red output of the first scenario: `assert not outcome.signalled` failed with
  the shim's census showing the supervisor, the held child (`S`) and the shim;
  of the second: `assert 1 == 7` with stderr
  `build-limits: the supervisor refused: launch-failed`.
- [x] Fix 1 commit (`KillMode=mixed`): the first scenario passes.
- [x] Fix 2 commit (restarting refusal): the second scenario passes.
- [x] Gates on head `df13251` through scrutineer: `make check-fmt`, `make lint`,
  `make typecheck`, `make markdownlint`, `make nixie`, `make test` (1748
  passed) and `make test-systemd-user` (24 passed).
- [x] Review-fix candidate gates through scrutineer: `make check-fmt`,
  `make lint`, `make typecheck`, `make markdownlint`, `make nixie`, `make test`
  (1748 passed, 25 skipped) and `make test-systemd-user` (24 passed).
- [x] PR 21 opened as a draft; hosted CI green; completeness assessment
  (`killmode-completeness-1`) answered with one documentation correction.
- [x] Review follow-up: the current completeness assessment is clear, the three
  inline findings are resolved, and the developer-documentation warning is
  reconciled. The proof assessment identified assertion gaps; the expanded
  property exposed repeated `Shutdown` effects from an already-closed admission.
  `Admission.begin_closing()` now returns no effects after its first close.
  The focused closing tests pass (12 passed).
- [x] Candidate `db02b38` passed the sequential gates before commit:
  `make check-fmt`, `make lint`, `make typecheck`, `make markdownlint`,
  `make nixie`, `make test` (1750 passed, 25 skipped) and
  `make test-systemd-user` (24 passed, both restart-during-placement scenarios
  passed). Logs use the `-13.out` suffix under `/tmp`.
- [x] The mutation control above rejected the forced `launch-failed` result at
  the named graceful witness.
- [ ] Run the final exact-head gates, renew the proof assessment on the pushed
  head, then continue the review loop to merge.
- [ ] Review loop to a mergeable state.

## Surprises & discoveries

- The held launch child is not killed by the control-group `SIGTERM`; it
  inherits the supervisor's handler. Only `busctl` is lost.
- Without the mixed kill mode, `TimeoutStopSec=infinity` also lets any
  `SIGTERM`-ignoring leftover hold a stop open forever.
- The signal handler only records intent, so the failed placement is seen before
  `Admission.closing` is set. A fix that tested `closing` alone would miss the
  incident.

## Decision log

- 2026-10-19: refuse as `restarting` rather than retry the placement (see
  Design).
- 2026-10-19: leave the cache unit's kill mode alone (see Design).
- 2026-10-20: honour a pending immediate stop before a failed placement can
  trigger a graceful-restart shutdown and cause adapters to retry.
- 2026-10-20: give the nested Cargo acceptance test the same 1800-second
  timeout as its subprocess wait; healthy shared compile admission can queue
  this test beyond the suite's 30-second default.

## Outcomes & retrospective

Delivered: `KillMode=mixed` in the unit contract (fixture, contract test,
guide, ADR 001 amendment, migration note), and the supervisor now refuses a
placement that fails during a graceful stop, requested or begun, as
`restarting`.

Notes for the reviewer:

- An experiment to confirm that `daemon-reload` alone makes a rewritten unit's
  new kill mode apply to the next stop was denied by the harness (it needed a
  write under `/run/user/1000/systemd/user`) and was not retried. The guide
  tells operators to reload and then read `KillMode` back with
  `systemctl --user show`, which does not depend on that claim.
- The first red commit's shim lacked an `import os`; it was found when Fix 1 was
  gated and folded back into the red commit.
- Deferred: the held launch child could reset its inherited signal handlers
  before the barrier; it is harmless today and out of scope.
