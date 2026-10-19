# Remove the cause of the flaky descriptor tests

This ExecPlan is a living document. Update Progress, Surprises and discoveries,
Decision log and Outcomes and retrospective as work proceeds.

Status: COMPLETE, awaiting review and merge.

## Purpose and big picture

Tests that compare open file descriptors against a baseline, or that apply
descriptor pressure, failed intermittently under xdist and passed alone. Recent
CI flakes were `test_recursive_jobserver[fifo]`, `test_send_pressure`,
`test_request_transactions[observe]`, `test_service_metrics_ownership[start]`,
`test_unreleased_disconnect` and
`test_queue_budget_refuses_without_health_loss_and_recovers`.

The cause is test-owned objects that hold a descriptor and are closed only when
the garbage collector runs. The collector runs during whichever test the xdist
worker happens to be executing, so that test sees descriptors that are not its
own. After this change every owner closes deterministically at teardown, and a
guard fails the test that leaks rather than a later, innocent one.

Observable result: `make test` passes repeatedly; reverting any one teardown
makes the guard fail the leaking test by name.

## Constraints

- Do not weaken, skip or retry descriptor tests, and do not paper over the leak
  with `gc.collect()`.
- Production code changes only where it lacked a `close()` path.
- No file over 400 lines; en-GB-oxendict spelling.

## Diagnosis (evidence)

Method: a temporary plugin (outside the repository) disabled the collector for
each test, diffed `/proc/self/fd` around the whole test protocol, then ran
`gc.collect()` to classify each new descriptor as closed-by-collector or never
closed. A second run used `-X dev -W error::ResourceWarning` with
`PYTHONTRACEMALLOC=15` to find objects freed by reference counting unclosed.

Findings on `047d183` (single process, `-n0`):

| Owner                                                                             | Descriptor        | Tests                                                                                                             | Closed by                     |
| --------------------------------------------------------------------------------- | ----------------- | ----------------------------------------------------------------------------------------------------------------- | ----------------------------- |
| `Supervisor` built by `harness_for` (selector)                                    | epoll             | 9 in `test_cache_recovery.py`, 5 in `test_cache_recovery_composition.py`, 2 in `test_cache_recovery_transport.py` | collector                     |
| `Supervisor` built by `_restarted` (selector)                                     | epoll             | 7 in `test_cache_recovery_reclaim.py`                                                                             | collector                     |
| Accepted request connection of the stalled-transport test                         | socket            | 1                                                                                                                 | collector                     |
| `TokenPool`                                                                       | device, wake pipe | 5 in `test_pool.py`                                                                                               | nobody (raw integers)         |
| Supervisor started by `_started` (signal self-pipe)                               | 2 pipes           | 1 in `test_supervisor_startup.py`                                                                                 | nobody                        |
| `Popen(stderr=PIPE)` of the fake sccache server                                   | pipe              | 2 in `test_fake_sccache.py`                                                                                       | collector (`ResourceWarning`) |
| `systemctl restart` and build `Popen` pipes in `test_graceful_restart_systemd.py` | pipes             | 5 (opt-in `systemd_user` scenarios)                                                                               | collector (`ResourceWarning`) |

The last row was not visible to the first two probes, because the scenarios are
skipped without `BUILD_LIMITS_HOST_ACCEPTANCE=1`; the guard found it on the
first `make test-systemd-user` run after it landed.

All 29 descriptor-diff entries trace to the first four rows. `Supervisor` holds
reference cycles (lambdas that capture `self`), so its selector survives until
a cyclic collection. The production code had no way to release the pool, the
listener, the signal self-pipe or the selector: it relied on process exit.

## Design

1. Production: `TokenPool.close()` and `Supervisor.close()`
   (`buildlimits/supervisor_shutdown.py`), called from `buildlimits/service.py`
   on every exit path. The pool never closes its device beneath a reader thread
   blocked in `read`; the thread closes it when the read returns, so a reused
   descriptor number can never be read.
2. Tests: `Harness` is a context manager; the `recovery_harnesses` factory
   fixture and a general `owned` `ExitStack` fixture unwind at teardown.
3. Guard: `tests/descriptor_guard.py`, registered as a plugin
   (`-p descriptor_guard`, with `pythonpath = ["tests"]`) so its autouse
   fixture also covers the package doctests, plus `filterwarnings` in
   `pyproject.toml`.

## Decision log

- Guard choice: a per-test descriptor diff with the cyclic collector suspended,
  not a warnings filter alone. Warnings fire when an object is finalized, which
  is when the collector happens to run, so they blame the wrong test under
  xdist and miss objects that never finalize. A per-test diff of
  `/proc/self/fd` is local to one process, so xdist workers cannot disturb each
  other, and with the collector suspended a cycle-held object cannot vanish
  mid-test, which makes the verdict independent of allocation timing. The
  filters (`error::ResourceWarning`,
  `error::pytest.PytestUnraisableExceptionWarning`) remain as the second net
  for objects freed by reference counting.
- Descriptors compared as (number, link target) so a reused number is seen.
- PR #14's `gc.collect()` in `open_fds` is removed. It masked leaks by closing
  them mid-test, which the guard now reports; with the owners closing at
  teardown the baseline comparison no longer needs it. Full suite passes
  without it (`1644 passed`).
- Existing ad hoc `selector.close()` calls in other tests are left as they are:
  they already close deterministically and the guard covers regressions.
  Converting them to `Supervisor.close()` is optional follow-up.

## Separate timing flake found on the way

Run 2 of 5 repeated `make test` runs failed
`test_incomplete_truncation_does_not_supply_a_policy_body[eof-missing-body]`
(`TimeoutError` instead of `TruncatedFrameError`). It is not a descriptor leak:
the receiving socket carried a 10 ms total frame deadline in a forked child,
which expires when a loaded worker is descheduled between header and body,
although every byte and the EOF are already queued. The EOF cases now use a 1 s
hang guard; the deadline cases keep 10 ms, since expiry is what they test. The
snapshots for the two EOF cases record the new limit.

## The in-flight descriptor limit (ETOOMANYREFS)

Most of the named flakes surface as `DescriptorSendPressureError`, which wraps
`ETOOMANYREFS` from `socket.send_fds`. Linux refuses an `SCM_RIGHTS` send when
the sending user's count of descriptors in flight (sent, not yet received, and
the carrying socket not yet closed) exceeds the sender's soft `RLIMIT_NOFILE`
(`too_many_unix_fds`, `net/unix/scm.c`). The isolated children run with a soft
limit of 64, and the count is per user, so it is shared by every xdist worker
and by any other process of the same user.

Confirmed by experiment (scratch script, kernel 6.12): with 120 rights left in
flight on one socketpair, a child at soft limit 64 fails to send one descriptor
with `ETOOMANYREFS`; once the rights are received, the same send succeeds.

Why it was intermittent in this suite: a test that sends rights and then leaves
its socket (or the supervisor holding it) to the collector keeps those rights
in flight until the collector runs, so the per-user count grows across tests
and workers. Deterministic closing returns them when the test ends.

Evidence, 12 full-suite runs each at `-n 6` on this host, identical load:

| Tree                                                 | Runs failed | Failures                                                                                                                                                       |
| ---------------------------------------------------- | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `origin/main` (`a3fd138`, with #14's `gc.collect()`) | 5 of 12     | 4 runs with `DescriptorSendPressureError` in `test_frame_receive_ceiling_is_exact` and `test_post_registration_fault_rolls_back_every_owner[observe]`, 1 other |
| this branch                                          | 0 of 12     | none                                                                                                                                                           |

One earlier run on this branch, while a second agent's suite was running on the
same user account, also failed two isolated children with the same error. That
is the residual route: a process outside this repository holding more than 64
rights in flight. Nothing in this suite can close descriptors it does not own,
and a CI runner has no such neighbour.

Per-test footprint is small (at most 8 rights per send; the 253-descriptor
cases are refused before sending), so the in-suite contribution comes from
lingering sockets, not from a single test's peak.

## Progress

- [x] Worktree `fix/test-descriptor-leaks` created from `origin/main`.
- [x] Diagnosis recorded above.
- [x] Production `close()` paths with tests.
- [x] Test teardown for harnesses, pools and fake sccache.
- [x] Red evidence: guard applied to unfixed `ce7d080` (which already carries
  the `gc.collect()` symptom patch of #14) reports 29 teardown errors.
- [x] Guard commit, developers' guide, rebased onto `ce7d080`, `gc.collect()`
      removed.
- [x] Gates on the rebased head (`make all`, markdownlint, nixie, audit,
  validate pass; 5 of 5 `make test` runs pass); `make test-systemd-user`: 21 of
  22 pass, and `test_nested_cargo` hits its 30 s timeout while the shared
  compile queue is saturated (capacity 4, held 4, about 140 waiting), which is
  environmental. Re-run for each later candidate, last recorded in the review
  round below.
- [x] Published as PR 16; hosted CI green; ExecPlan completeness assessment
  clear (second round, after the guard became a plugin and `TokenPool.__init__`
  gained rollback).
- [x] Review round 1 (CodeRabbit, Codex): `TokenPool.close()` now decides from
      an
  `_is_reading` flag set under the lifecycle lock, not from a timed `join`;
  `Supervisor.close()` runs every release through an `ExitStack`; the signal
  state a host owned is restored rather than reset; the service logs a failed
  release instead of changing its exit status; added a `Supervisor.close()`
  integration test, a Hypothesis state machine for the pool, and the
  `cache_recovery_machines` helper inventory.
- [ ] Review convergence.

## Surprises and discoveries

- PR #14 merged while this work was in progress; `origin/main` moved to
  `ce7d080`. See the Outcomes section for the decision on its `gc.collect()`.
- The pool tests leaked raw integer descriptors that no collector would ever
  close, so they polluted every later test in the worker for good.
- A `Popen` with `stderr=PIPE` leaks its reader unless the process object is
  entered as a context manager or the pipe is closed.

## Outcomes and retrospective

- Every diagnosed owner now closes at teardown; production has
  `TokenPool.close()` (with constructor rollback) and `Supervisor.close()`.
- The guard failed 29 tests on the unfixed tree and fails none on the fixed one;
  its own tests include an unclosed recovery harness and a leaking doctest run
  through the real plugin registration.
- Flake count: 12 full-suite runs at `-n 6` on this host failed 5 on
  `origin/main` and 0 on this branch; scrutineer's repeated `make test` runs (5
  per candidate across four candidates) showed 0 failures after the EOF
  deadline fix, apart from one run that coincided with another agent's suite.
- #14's `gc.collect()` is removed, not retained.
- Review-driven corrections: the guard moved from a conftest fixture to a plugin
  so doctests are covered; `TokenPool.__init__` rolls back; the developers'
  guide no longer claims the guard sees wider-scoped fixtures.
- Deferred: converting the older ad hoc `selector.close()` calls to
  `Supervisor.close()`; the neighbour-process in-flight limit;
  `test_nested_cargo`'s 30 s timeout under a saturated shared compile queue.
