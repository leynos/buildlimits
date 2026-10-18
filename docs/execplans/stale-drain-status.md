# Tell agents when an automatic stale-drain recovery is pending

This ExecPlan is a living document. Status: IN PROGRESS.

## Purpose

On host rohga (2026-10-18, supervisor at `7445bf0`) `build-limits-status`
showed a retained `live_work` drain, waiting requests, no `cache recovery` line
and a mandatory-sounding `required recovery action: Run build-limits-recover`.
For up to five minutes (`RECOVERY_STALE_DRAIN_SECONDS` = 300) the automatic
trigger was about to act, yet host agent instructions say to escalate exactly
that picture, so an agent escalated a normal wait. The supervisor behaved as
designed; only the operator surface was wrong. After this change status tells
the truth: while the stale proof is the only missing clause it prints
`cache recovery idle: automatic recovery may start in Ns` (the wording agents
already read as "keep waiting"), and the recovery action says automatic
recovery is pending and the operator command is a fallback.

## Constraints

- Do not change recovery timing or behaviour. Shortening the 300 s deadline
  when retention covers most of the pool is deferred (see Outcomes).
- Do not edit `tests/descriptor_helpers.py` or the harness descriptor teardown.
- Status must not promise a recovery the trigger will not perform: the
  countdown comes from the same clauses `starvation()` and `refusal()` use, via
  one shared query. `blocked()` gates the stop that follows an opening, not the
  opening itself, so it is excluded.
- No host installs, no `build-limits-recover`, no service control.

## Design

Conformance basis: no Terms of Reference exist; governing documents are
`docs/users-guide.md`, `docs/developers-guide.md` section 9, and ADR 001 for
the status document conventions.

1. `buildlimits/recovery_outlook.py` gains `opens_in(reading)`, built on
   `recovery_starvation.permits_but_for_time`: seconds until the automatic
   trigger could open, `0.0` when it would open now, and `None` when time alone
   will not open it (not `sccache`, not `starved`, fault is not `live_work`,
   `refusal()` non-empty, no server-drain token, or no waiting request).
   `starvation()` is refactored onto the same private clause check, so
   `starvation(r) is not None` exactly when `opens_in(r) == 0`. The value is
   `max(stale remainder, cooling remainder)`; the stale remainder is `0` when
   the prompt proof already holds. `blocked()` gates the stop, not the opening,
   so it is deliberately not part of the countdown. Status reads the drain's
   start (`RetainedDrainAge.since`) through `timing.seconds_since`, which
   records nothing.
2. `CacheRecovery.describe` adds `automatic` (mode and policy permit the
   trigger), `opens_in` (idle transactions only) and `pending` (an automatic
   recovery is due or under way and has attempts left). The idle line shows
   `opens_in` when positive and honours an explicit `null` as "no countdown";
   only a legacy document without the key falls back to `cooling_for`, which is
   suppressed when `automatic` is false. A resting trigger with nothing
   retained therefore no longer prints a line (review finding on PR #15: it
   would also print beside a faulted manager).
3. Wording lives in `buildlimits/drain_guidance.py`. `ServerWatch` takes an
   `automatic_recovery_pending` callable. Its `recovery_action` becomes a live
   property: the mandatory text, or, for `live_work` while recovery is pending,
   a fallback text. The journal line for the pending case reads
   `drain recovery is pending automatically; operator action is only a
   fallback: …`,
   and the mandatory case keeps `drain requires operator action: …`. The
   `DrainLoop` internal-error path never involves cache recovery and keeps its
   mandatory wording.
4. `status_render` labels the line `fallback recovery action` when the document
   says so (`recovery_action_is_fallback`).

## Milestones

1. Plan committed.
2. Red tests (unit, behavioural, property, syrupy snapshot, boundaries 299 and
   300 as literals, incident shape through the real supervisor wiring).
3. Implementation to green.
4. Documentation: users' guide, developers' guide, migration note.
5. Gates via scrutineer, PR, babysit to equilibrium, squash merge.

## Verification plan

Invariant I1: for any reading, if `opens_in` is a positive number, advancing
`now` and `drain_age` by that number (all else equal) makes `starvation()`
fire. Invariant I2: if any non-time clause fails, `opens_in` is `None`.
Invariant I3: `starvation(r) is not None` iff `opens_in(r) == 0.0`. Method:
hypothesis properties on `StarvationReading` with half-second grids so float
arithmetic is exact; non-vacuity is argued by asserting the generator reaches
positive countdowns (`hypothesis.event`/`assume` counts) and by mutation of the
300 literal during the red phase. Boundaries 299 and 300 are literals in
parametrized tests. Incident shape: the production supervisor over a busy host.

## Progress

- [x] Worktree `fix/stale-drain-status` from `origin/main` at `ce7d080`.
- [x] ExecPlan written.
- [x] Red tests (31 of 32 failed before the code; log kept in the scratchpad).
- [x] Implementation.
- [x] Documentation.
- [ ] Gates, PR, review, merge.

## Surprises & discoveries

- The existing cooling line also appears under `manual` policy after an
  operator recovery, promising an automatic start that never comes. Fixed by the
  `automatic` flag.
- `cache_recovery.py` and `server_watch.py` had no headroom under the 400-line
  module limit, so the countdown moved to `recovery_outlook.py`, the guidance to
  `drain_guidance.py` and the stop guard to `recovery_stop.py`.

## Decision log

- Reuse the exact line
  `cache recovery idle: automatic recovery may start in Ns`; no host
  instruction change is needed for it.
- Countdown covers cooldown by taking the later of the two instants.

## Outcomes & retrospective

Deferred, deliberately out of scope: shortening the 300-second deadline when
retention covers most of the pool.
