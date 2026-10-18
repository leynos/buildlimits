# Remove the cause of the flaky descriptor tests

This ExecPlan is a living document. Update Progress, Surprises and discoveries,
Decision log and Outcomes and retrospective as work proceeds.

Status: IN PROGRESS.

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

| Owner                                                     | Descriptor        | Tests                                                                                                             | Closed by                     |
| --------------------------------------------------------- | ----------------- | ----------------------------------------------------------------------------------------------------------------- | ----------------------------- |
| `Supervisor` built by `harness_for` (selector)            | epoll             | 9 in `test_cache_recovery.py`, 5 in `test_cache_recovery_composition.py`, 2 in `test_cache_recovery_transport.py` | collector                     |
| `Supervisor` built by `_restarted` (selector)             | epoll             | 7 in `test_cache_recovery_reclaim.py`                                                                             | collector                     |
| Accepted request connection of the stalled-transport test | socket            | 1                                                                                                                 | collector                     |
| `TokenPool`                                               | device, wake pipe | 5 in `test_pool.py`                                                                                               | nobody (raw integers)         |
| Supervisor started by `_started` (signal self-pipe)       | 2 pipes           | 1 in `test_supervisor_startup.py`                                                                                 | nobody                        |
| `Popen(stderr=PIPE)` of the fake sccache server           | pipe              | 2 in `test_fake_sccache.py`                                                                                       | collector (`ResourceWarning`) |

| `systemctl restart` and build `Popen` pipes in `test_graceful_restart_systemd.py` | pipes | 5 (opt-in `systemd_user` scenarios) | collector (`ResourceWarning`) |

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
3. Guard: `tests/descriptor_guard.py`, applied to every test by an autouse
   fixture in `tests/conftest.py`, plus `filterwarnings` in `pyproject.toml`.

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

## Progress

- [x] Worktree `fix/test-descriptor-leaks` created from `origin/main`.
- [x] Diagnosis recorded above.
- [x] Production `close()` paths with tests.
- [x] Test teardown for harnesses, pools and fake sccache.
- [x] Red evidence: guard applied to unfixed `ce7d080` (which already carries
  the `gc.collect()` symptom patch of #14) reports 29 teardown errors.
- [x] Guard commit, developers' guide, rebased onto `ce7d080`, `gc.collect()`
      removed.
- [ ] Gates, repeated runs, PR.

## Surprises and discoveries

- PR #14 merged while this work was in progress; `origin/main` moved to
  `ce7d080`. See the Outcomes section for the decision on its `gc.collect()`.
- The pool tests leaked raw integer descriptors that no collector would ever
  close, so they polluted every later test in the worker for good.
- A `Popen` with `stderr=PIPE` leaks its reader unless the process object is
  entered as a context manager or the pipe is closed.

## Outcomes and retrospective

To be completed once the gates and repeated runs finish.
