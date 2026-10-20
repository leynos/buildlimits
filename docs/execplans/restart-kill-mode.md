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

### Review follow-up: bounded tracing

The configured PR observability check requires spans at process boundaries. Add
OpenTelemetry spans for supervisor scope placement and adapter admission retry.
The adapter initializes its SDK only after the first restart; the supervisor
records placement spans through the same non-blocking journal transport as
metrics. The exporter allows only fixed operation, outcome, failure-category
and stop-mode vocabularies, a capped retry count, bounded elapsed milliseconds
and fixed-width generated trace identifiers. It excludes compiler payloads,
paths, credentials and raw exceptions, and drops telemetry failures without
changing admission or compiler results. The real-systemd scenarios assert the
mixed-mode placement and the legacy `control-group` restart-refusal spans.

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
- [x] The tree committed as `dee4ed9` passed the sequential gates before
  commit: `make check-fmt`, `make lint`, `make typecheck`, `make markdownlint`,
  `make nixie`, `make test` (1750 passed, 25 skipped) and
  `make test-systemd-user` (24 passed, both restart-during-placement scenarios
  passed). The gate run included the ExecPlan evidence committed in `dee4ed9`;
  logs use the `-14.out` suffix under `/tmp`.
- [x] The mutation control above rejected the forced `launch-failed` result at
  the named graceful witness.
- [x] Add bounded OpenTelemetry spans at scope placement and adapter restart
  retry, with exporter failure isolation and tests for both systemd kill modes.
- [x] Refactor the tracing follow-up after its first lint run: split the retry
  state and scope-placement work into focused modules, group supervisor
  observation ports, classify trace failures without retaining exception data,
  and complete the affected docstrings.
- [x] Run the sequential candidate gates after the latest lint remediation.
  An earlier tracing candidate's `make check-fmt` passed; `make lint` stopped
  on 29 findings
  (`/tmp/lint-ecd20a52-58e4-42d8-8324-6e18a28122f7-fix-restart-kill-mode-16.out`).
  The next candidate passed `make check-fmt`, then lint stopped on 11 findings
  (`/tmp/lint-ecd20a52-58e4-42d8-8324-6e18a28122f7-fix-restart-kill-mode-17.out`).
  After fixing those findings, candidate 18 passed `make check-fmt` but lint
  reported two issues in `retry_request`: branch complexity and a named re-raise
  (`/tmp/lint-ecd20a52-58e4-42d8-8324-6e18a28122f7-fix-restart-kill-mode-18.out`).
  The loop now continues explicitly after retryable failures and uses a bare
  re-raise. Candidate 19 passed `make check-fmt`, then the 400-line file cap
  flagged `tracing.py` (403 lines) and `test_adapter_restart.py` (407 lines)
  (`/tmp/lint-ecd20a52-58e4-42d8-8324-6e18a28122f7-fix-restart-kill-mode-19.out`).
  The journal exporter/schema and retry-tracing tests now live in focused
  modules. Candidate 20 passed `make check-fmt`; lint reported type-check
  suppressions, redundant future annotations, test assertion clarity findings,
  and a dispatch style finding
  (`/tmp/lint-ecd20a52-58e4-42d8-8324-6e18a28122f7-fix-restart-kill-mode-20.out`).
  The tracing adapter now uses explicit SDK protocols, tests assert each
  bounded field with a reason, and the retry test double updates its state
  directly. Candidate 21 passed `make check-fmt`; lint found two
  `collections.abc` imports that must be hidden from runtime imports
  (`/tmp/lint-ecd20a52-58e4-42d8-8324-6e18a28122f7-fix-restart-kill-mode-21.out`).
  Candidate 22 passed `make check-fmt`, then lint rejected `typing.Mapping` and
  `typing.Sequence` as banned generics
  (`/tmp/lint-ecd20a52-58e4-42d8-8324-6e18a28122f7-fix-restart-kill-mode-22.out`).
  Candidate 23 restores the project-standard `collections.abc` aliases inside
  `TYPE_CHECKING` blocks. Ruff and Pylint passed, then Skylos reported callback
  methods and collaborator calls that the repository's graph does not resolve,
  plus the unused `_placement_stop_mode` alias
  (`/tmp/lint-ecd20a52-58e4-42d8-8324-6e18a28122f7-fix-restart-kill-mode-23.out`).
  The retry and placement transitions now live in their owning modules, and
  the unused alias is removed. Candidate 24 reduced the strict Skylos findings
  to four cross-module method calls (`/tmp/skylos-pr21-candidate24.out`).
  Candidate 25 makes those calls explicit on the concrete internal classes
  while retaining the journal transport's `_emit` override seam; the strict
  Skylos 4.33.2 scan passes (`/tmp/skylos-pr21-candidate25.out`). The full
  candidate's `make check-fmt` passed, while lint found a missing return
  description and two imports used only as types
  (`/tmp/lint-ecd20a52-58e4-42d8-8324-6e18a28122f7-fix-restart-kill-mode-24.out`).
  Candidate 26 records those return semantics and moves the imports into
  `TYPE_CHECKING`. Its check-format gate passed; Vulture then reported the test
  sinks' `_emit` overrides as unused
  (`/tmp/lint-ecd20a52-58e4-42d8-8324-6e18a28122f7-fix-restart-kill-mode-25.out`).
  Candidate 27 records trace fields through the same real Unix datagram
  transport used by the service and uses a regular file as an invalid socket
  path for failure coverage, removing unreferenced test overrides without
  suppressions. Its `make check-fmt` passed; lint then found an escaped newline
  in the helper's doctest and a blank line after one test docstring
  (`/tmp/lint-ecd20a52-58e4-42d8-8324-6e18a28122f7-fix-restart-kill-mode-26.out`).
  Candidate 28 marks the doctest as a raw string and removes the extra blank
  line. Its check-format gate passed, then lint stopped at seven duplication
  families
  (`/tmp/lint-ecd20a52-58e4-42d8-8324-6e18a28122f7-fix-restart-kill-mode-27.out`).
  Candidate 29 shares the two metrics-test supervisor fixtures, consolidates
  repeated retry-test requests and retry-wait cleanup, and factors
  tracing-local identifier validation and failure-count increments. It also
  asserts the completed trace for an immediate-stop placement. Formatting and
  the full sequential gates remain pending. The first check-format pass
  succeeded; lint found stale interrupt exception documentation, missing
  return/yield descriptions, a complex identifier predicate, and fixture
  imports that Ruff treats as redefinitions
  (`/tmp/lint-ecd20a52-58e4-42d8-8324-6e18a28122f7-fix-restart-kill-mode-28.out`).
  Candidate 30 documents interruption propagation at the shared cleanup
  boundary, spells out return/yield contracts, uses an early-return identifier
  validator, and moves the observing supervisor fixture into `conftest.py`. The
  candidate has been formatted; all gates must restart at check-format. Its
  check-format gate passed, then lint found a trivial span attribute wrapper
  and requested structural pattern matching for identifier validation
  (`/tmp/lint-ecd20a52-58e4-42d8-8324-6e18a28122f7-fix-restart-kill-mode-29.out`).
  Candidate 31 moves the bounded update logic into the span's `set` method and
  handles booleans before integer identifiers in a `match`. Its check-format
  gate passed and lint cleared those findings, then duplication detected two
  families: repeated type-only import scaffolding around standard library
  annotations, and matching placement-failure bookkeeping
  (`/tmp/lint-ecd20a52-58e4-42d8-8324-6e18a28122f7-fix-restart-kill-mode-30.out`).
  Candidate 32 keeps test annotation imports available at runtime, as allowed
  by the test lint policy, and consolidates shared failed-placement handling.
  Its check-format and lint gates passed; typecheck then caught a stale test
  import left behind when `retry_request` moved to `adapter_retry`
  (`/tmp/typecheck-ecd20a52-58e4-42d8-8324-6e18a28122f7-fix-restart-kill-mode-31.out`).
  Candidate 33 imports the retry API from its owning module. All gates must
  restart at check-format. Candidate 33 passed check-format, lint, typecheck,
  Markdown and Mermaid validation; the unit suite found 13 failures in the
  journal doctest/message, telemetry-aware service factories, and the
  supervisor effects group double
  (`/tmp/test-ecd20a52-58e4-42d8-8324-6e18a28122f7-fix-restart-kill-mode-32.out`).
  Candidate 34 corrects the journal example and message contract, updates the
  service doubles for `SupervisorTelemetry`, and gives the groups double the
  configuration required by its real `cache_notice` path. All gates must
  restart at check-format. The first five gates passed; the unit suite found
  one success-notification expectation that now observes a fallback notice from
  the fake's default `sccache` mode
  (`/tmp/test-ecd20a52-58e4-42d8-8324-6e18a28122f7-fix-restart-kill-mode-33.out`).
  Candidate 35 configures that test's fake for direct mode, the scenario it
  intends to exercise. Its 37-path dirty worktree, based on commit `4a4bbe4`,
  passed check-format, lint, typecheck, Markdown lint, Mermaid validation, unit
  tests (1757 passed, 25 skipped), systemd-user tests (24 passed), and audit
  (no known vulnerabilities; the local `buildlimits` package was not found on
  PyPI and could not be audited). These gates did not run against the clean
  `4a4bbe4` commit. The tested worktree was later published as `a9883dd`.
  Systemd-user tests took 876.88 seconds while nested Cargo work queued
  normally behind healthy shared compile admission. Logs use the `-34.out`
  suffix under `/tmp` for session `ecd20a52-58e4-42d8-8324-6e18a28122f7`.
- [x] Hosted CI on `a9883dd` failed at the Markdown spelling gate because
  `buildlimits/tracing_export.py` used `Recognised`; later CI steps were
  skipped. Act Validation passed. The spelling is corrected in the current
  worktree. On reviewed head `63a4b2d`, hosted
  [CI](https://github.com/leynos/buildlimits/actions/runs/39472151952) and
  [Act Validation](https://github.com/leynos/buildlimits/actions/runs/39472151597)
  both passed.
- [x] The integrated four-path worktree based on `a9883dd` passed the complete
  sequential local gate set. Its dirty-diff SHA256 is
  `11d188be4b2cf5dc1021e67d2574ac1be4b1f352a4e91594ff7e1ad767b8a757`.
  Formatting, lint, typecheck, Markdown lint, and Mermaid validation passed;
  unit tests passed (1760 passed, 25 skipped), systemd-user tests passed (24
  passed), and audit reported no known vulnerabilities. The package itself was
  not found on PyPI and could not be audited. Logs use the `-41.out` suffix
  under `/tmp` for session `ecd20a52-58e4-42d8-8324-6e18a28122f7`.
- [x] Review follow-up coverage now holds `Finished` until the retry span is
  exported during the adapter's `Admitted` reply handling. The test also runs
  through `retry_request` and checks that the post-admission fallback does not
  emit a duplicate span. Controlled SDK failures cover initialization,
  attribute updates, and span completion.
- [x] The first complete-gate rerun passed formatting, lint, and typecheck, then
  stopped in `make test`: `test_close_closes_remaining_descriptors` exceeded
  the one-second child-reap deadline under the parallel suite and leaked its
  error descriptor during failure cleanup. The test now allows ten seconds,
  matching the other bounded child-collection helper, and always abandons an
  unreaped child and closes its descriptors.
- [x] The next complete sequential run passed all eight gates on the candidate
  tree: formatting, lint, typecheck, Markdown lint, Mermaid validation, unit
  tests (1760 passed, 25 skipped), systemd-user tests (24 passed), and audit
  (no known vulnerabilities; `buildlimits` was not found on PyPI). The nested
  Cargo case queued for shared compile admission for about 21 minutes, then
  passed; admission remained healthy throughout. Logs use the `-45.out` suffix
  under `/tmp` for session `ecd20a52-58e4-42d8-8324-6e18a28122f7`.
- [x] Re-run the complete sequential gates after the exact-once retry-span
  assertion, child-reap cleanup, and corrected gate-attribution note.
- [ ] Renew the assessments for the reviewed head, reconcile the configured
  Observability row, and merge PR 21.
- [x] Hosted Actions runs on `dee4ed9` passed: Act Validation
  [39428388369](https://github.com/leynos/buildlimits/actions/runs/39428388369)
  and CI
  [39428388335](https://github.com/leynos/buildlimits/actions/runs/39428388335).
- [x] Assessments on `dee4ed9` are clear: completeness
  [6291733244](https://github.com/leynos/buildlimits/pull/21#issuecomment-6291733244)
  and bounded proof
  [6291772272](https://github.com/leynos/buildlimits/pull/21#issuecomment-6291772272).
- [x] Published review-fix head `2a8f45f` passed all eight sequential local
  gates. Unit tests reported 1,762 passed and 25 skipped; systemd-user tests
  reported 24 passed, including both restart-during-placement cases and nested
  Cargo; audit found no known vulnerabilities. The audit's `certifi` finding
  (`GHSA-6q48-6c7w-9gw2`) was fixed by locking `certifi` to `2026.10.19`.
  `buildlimits 0.1.0` remains unaudited because it is not on PyPI. Logs use the
  `-57.out` suffix under `/tmp` for session
  `ecd20a52-58e4-42d8-8324-6e18a28122f7`.
- [x] Hosted Actions on `2a8f45f` passed: CI
  [39488539175](https://github.com/leynos/buildlimits/actions/runs/39488539175)
  and Act Validation
  [39488539243](https://github.com/leynos/buildlimits/actions/runs/39488539243).
- [x] Completeness assessment on `2a8f45f` found no remaining in-scope
  correctness defect or unmet requirement. It requested that this exact-head
  gate evidence be added before closing the plan:
  [6300330753](https://github.com/leynos/buildlimits/pull/21#issuecomment-6300330753).
- [x] CodeRabbit's Unit Architecture error in the older walkthrough
  [6269410130](https://github.com/leynos/buildlimits/pull/21#issuecomment-6269410130)
  remains valid at `0de0e9f`: the exporter called `JournalSink.emit_trace`
  through the class, bypassing an injected instance override. The new focused
  regression failed at the expected assertion on that head; its log is
  `/tmp/focused-test-ecd20a52-58e4-42d8-8324-6e18a28122f7-fix-restart-kill-mode.out`.
- [x] The first full candidate run passed format checking but Vulture treated a
  test subclass override as unused. Replace that fixture with an instance
  method mock so the regression exercises the same dispatch seam without a
  dead-code exception or suppression.
- [x] The exporter now dispatches through a module-private `_TraceSinkPort`,
  and the focused injected-sink regression passes. All eight sequential gates
  passed on the candidate diff based on `0de0e9f`: unit tests (1,763 passed, 25
  skipped), systemd-user tests (24 passed, 1,727 deselected), and audit (no
  known vulnerabilities; the local `buildlimits` package was not found on
  PyPI). The nested Cargo case queued on the shared package-cache lock before
  passing; build admission remained healthy. Logs use the `-60.out` suffix under
  `/tmp` for session `ecd20a52-58e4-42d8-8324-6e18a28122f7`.
- [x] On `d792556`, CodeRabbit accepted completeness
  [6301397897](https://github.com/leynos/buildlimits/pull/21#issuecomment-6301397897),
  accepted the bounded property evidence
  [6301437521](https://github.com/leynos/buildlimits/pull/21#issuecomment-6301437521),
  and marked Observability satisfied
  [6301467887](https://github.com/leynos/buildlimits/pull/21#issuecomment-6301467887).
  The hosted CI and Act Validation runs also passed on that head:
  [39495788642](https://github.com/leynos/buildlimits/actions/runs/39495788642)
  and
  [39495788631](https://github.com/leynos/buildlimits/actions/runs/39495788631).
- [x] The walkthrough reconciliation
  [6301514837](https://github.com/leynos/buildlimits/pull/21#issuecomment-6301514837)
  accepts Unit Architecture as Passed and narrows Developer Documentation to
  an explicit runtime-dependency sentence. Add that sentence and pass
  `make check-fmt`, `make markdownlint`, and `make nixie` on the docs diff.
- [x] Publish the documentation follow-up as `14607d5`. On the exact head,
  `make check-fmt`, `make markdownlint`, and `make nixie` passed; hosted CI
  [39498592602](https://github.com/leynos/buildlimits/actions/runs/39498592602)
  and Act Validation
  [39498592791](https://github.com/leynos/buildlimits/actions/runs/39498592791)
  also passed. CodeRabbit confirms Developer Documentation as Passed and
  reconciles the walkthrough to 0 errors, 0 warnings, and 15 passed checks
  [6301704387](https://github.com/leynos/buildlimits/pull/21#issuecomment-6301704387).
  The generated walkthrough itself remains stale at 1 error, 1 warning, and 13
  passed checks; individual row updates are unavailable.
- [ ] Renew the completeness assessment against the final published head, await
  current-head formal review, reconcile any remaining finding or pre-merge row,
  and merge PR 21.

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
guide, ADR 001 amendment, migration note), the supervisor now refuses a
placement that fails during a graceful stop, requested or begun, as
`restarting`, and bounded restart and placement spans record their outcomes
through the journal.

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

## Revision note

2026-10-20: Record the typing-import, dead-code, and tracing follow-up lint
findings and their remediation. Candidate 35 was a 37-path dirty worktree based
on `4a4bbe4`, later published as `a9883dd`; its gates ran on the dirty
candidate. See Progress for the later integrated gate results.
