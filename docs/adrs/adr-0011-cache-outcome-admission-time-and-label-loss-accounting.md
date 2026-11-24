# Architectural decision record (ADR) 011: cache-outcome admission time and label-loss accounting

## Status

Proposed. The decision is implemented on the issue #68 branch; accept this
record when that work merges. It refines the outcome store and recorder in
[ADR 005](adr-0005-cache-outcome-recording.md) and keeps the evidence boundary
in [ADR 007](adr-0007-cache-outcome-observation-port.md).

## Date

2026-11-23.

## Context and problem statement

The cache-hit prediction benchmark refuses to score a history it cannot trust.
[PR #61](https://github.com/leynos/buildlimits/pull/61) reported the host
snapshot behind [issue #60](https://github.com/leynos/buildlimits/issues/60):
10,159 cache-outcome events, 6,845 distinct identities, but only 10 attributed
`hit` or `miss` labels, about 0.10% coverage against a 2,000-label gate. The
benchmark reported `insufficient data`, which was correct. Issue #68 asks where
the other labels went and whether a prediction can be shown to have used only
information available before admission.

The same gap appeared when this decision was prepared. A copy of the host store
taken at 2026-11-23T21:08Z held 15,846 outcome rows, all `unattributed`, with
13,412 distinct keys and no window summaries. The user journal over a
fifteen-minute window of the same load held 3,801 admitted requests (3,786
cached), 3,835 `cache_outcome` events and no completed statistics readings.
Every one of those events was `window_overflow` at 65 groups. A wider read over
the whole observation span showed the same pattern: 36,530 events, all
`window_overflow`, with 839 reading ticks skipped in the sampled quarter hour.

The cause is structural rather than a rare sampler fault. The recorder could
open a statistics read only when `queued == 0` and `cached_moving == 0`. On
this host the compile queue stayed several hundred requests deep, so the queue
clause never held. The bounded window then filled to its 64-entry limit and
flushed every launch as unattributed. Because a flush left the old statistics
baseline in place, the overflowed requests remained in sccache's cumulative
counters and the next window could not balance either. The recorder could not
even establish a first baseline while the host was busy, although a baseline is
only a counter snapshot and needs no motionless host.

The second gap is independent of label coverage. `OutcomeRecord` carried the
label time (`observed_at`) and a runtime, but no admission time. The benchmark
correctly refused to subtract the runtime from the label time: that bound does
not prove when the decision was taken, and using it would leak labels that
arrived after admission.

## Decision drivers

- Keep the observation-only boundary: recording must never influence
  admission, FIFO order, token accounting, scheduling or the live predictor.
- Never invent a label. An ambiguous counter change remains `unknown`, and a
  missing admission time is never replaced by an inferred one.
- Make label loss measurable from the data each layer actually holds: the
  observer's journal, the retained store, the snapshot and the scored events.
- Read old stores and snapshots without rewriting them, and make the rows that
  predate admission evidence explicitly unscorable.
- Keep every diagnostic count bounded by a closed vocabulary; no per-request
  debugging log.
- Preserve the sccache-specific aggregation contract. A per-request label is
  used only where the backend supports one.

## Decision

### D1: version the store with admission and classification evidence

The outcome store format becomes version 2. An `outcome` row gains three
optional fields:

- `admitted_at`: the UTC wall-clock moment the supervisor admitted the
  compilation, captured in `Groups.place` at the same point as the monotonic
  `entry.admitted`. It is read where admission is decided, not when the row is
  written and not by arithmetic on the label time.
- `outcome_reason`: the closed recorder reason that explains a non-label, or
  `none` for a sound label.
- `outcome_attribution`: `solo`, `unanimous` or `none`.

The admission time is carried from the admission decision through `Pending`,
`Launched`, the recorder's `_Launch` and the emitted `OutcomeRecord`. The
per-compilation cache key and the row itself are the correlation: the admission
observation and the label are written from the same launch object, so no
separate join can reorder them. A hard restart loses the in-memory window; no
row then claims an admission time it did not observe.

Version 1 rows remain readable and decode every new field as `None`. A `None`
admission time is not a zero and not a guess: the benchmark event keeps it
absent, `admission_moment` returns `None`, and the row is never scored. A
writer that compacts an old store writes a version 2 header while preserving
the old rows, so a version 2 file may contain version 1 rows; the decoder
validates each row's shape, not a header-wide assumption. The snapshot format
becomes version 2 as well; a version 1 snapshot loads with diagnostics
unavailable.

### D2: keep label-loss accounting in the snapshot, not in per-request logs

The snapshot carries one bounded `diagnostics` block. Its retained-store part
counts outcome rows by cache result, outcome reason, attribution, origin,
parser class, child sighting and sccache version, plus recorded and missing
admission times and the labelled total. Its history part names the archived and
sealed segments, the missing sequences, the retention floor and whether the gap
list was truncated.

The dataset command also records the observer's own counts from the user
journal: admitted requests by cache path and backend, published outcomes by
result, reason, attribution and origin, the writer's appended and dropped or
evicted rows, completed, skipped and failed readings, and the gate reasons the
journal witnessed. The four denominators stay distinct: all admitted requests,
cache-eligible compilations, retained `OutcomeRecord`s and scored outcomes. A
report states each one rather than dividing by whichever is convenient.

The snapshot loader reconciles the diagnostics before a report can print them:
every breakdown must sum to the outcome rows, the record totals must add up,
the admission counts must cover the same rows, and the labelled total must
equal the hit plus miss counts.

### D3: repair the observation gate without weakening attribution

Three changes are made to the quiet-point gate and the window:

- The `queued == 0` clause is removed. A queued request that starts during the
  statistics query is already detected by ADR 005 D3's straddle rule, which
  discards the whole reading. The clause added no safety and made a reading
  impossible on a host that keeps work queued continuously.
- Every reading, baseline included, requires `cached_moving == 0` and
  `server_drain == 0`. A baseline taken while a cached compilation is in flight
  captures counters that no later window accounts for: the launch is emitted
  with the baseline's own window, but its remaining counters land in the next
  window's delta. Holding both clauses clear means every delta is composed only
  of launches its window holds. The queue clause is the one that made a reading
  impossible on a continuously queued host.
- A window overflow resets the baseline. The overflowed launches are still in
  sccache's cumulative counters, so keeping the old baseline would make every
  later delta include requests that are no longer in the window.

The approved attribution rules are otherwise unchanged. A solo or unanimous
window can still produce a label; a mixed, ineligible, unbalanced or straddled
window stays `unknown` with its own reason.

## Consequences

- A controlled run against real sccache 0.14 produces exactly one `miss` and
  one `hit` from a cold then warm compilation, both with an admission time at
  or before the label. A concurrent mixed pair remains `unknown` with
  `mixed_window`; a declined probe is `declined`; a timeout and a generation
  change keep their own reasons.
- The recorder establishes a baseline at the first point where no cached group
  is moving and no detached work is draining. A continuously busy host may
  reach that point rarely; when it does not, the bounded window overflows and
  reports `window_overflow` rather than guessing. Under the measured continuous
  load, sccache's aggregate counters cannot identify an individual concurrent
  request, so the honest result is a low label rate with a visible reason
  distribution, not a target rate.
- Existing version 1 history remains readable but contributes no labels or
  admission times to the benchmark. An operator who wants new evidence must run
  the updated supervisor; no historical row is retroactively repaired.
- The store row grows by about 71 bytes, so segment boundaries and archive
  sizes move. Archive tests and byte budgets that assumed the version 1 row
  size are updated.
- The live service is not modified by this decision. Deploying the code and
  observing the new rows on the host remain operator actions.

## Alternatives considered

- **Treat an absent hit as a miss.** Rejected: it manufactures labels and is
  exactly the inference ADR 005 and issue #68 forbid.
- **Lower `minimum_labelled_events`.** Rejected: it changes the verdict, not
  the evidence.
- **Split a mixed counter delta between concurrent requests.** Rejected:
  sccache 0.14 exposes no per-request identity, so any split would be a guess.
- **Parse sccache server logs or child sightings as per-request labels.**
  Rejected: sccache can print `Cache hit` and `Cache miss` lines at
  `SCCACHE_LOG=debug`, but that is a debug log rather than a supported API, its
  wording is not part of the version contract, and it names the compiler
  output. Child sightings also miss short-lived work. A backend that offers a
  supported per-request signal can be added behind the observation port in ADR
  007.
- **Keep `queued == 0`.** Rejected: the measured host never satisfies it, and
  the straddle rule already provides the safety the clause was thought to add.
- **Take a first baseline while cached work is moving.** Rejected: the
  counters a moving compilation has not yet published land in the next window's
  delta, which no window accounts for. Holding the movement clauses for every
  reading is the only sound rule while sccache exposes only aggregate counters.
- **Flush the overflowed window and keep its baseline.** Rejected: the
  overflowed requests remain in the cumulative counters, so later windows
  cannot balance and the loss becomes permanent.

## Follow-up

- The benchmark continues to report `insufficient data` or
  `insufficient admission times` until genuine rows meet both gates.
- A later observation port may add a supported per-request backend signal for a
  backend that provides one. It must keep the mixed-window `unknown` result for
  sccache 0.14.
- Permanent host provisioning and the timer that archives history remain with
  [dev-env-rocky #424](https://github.com/leynos/dev-env-rocky/issues/424) and
  the archival work in [ADR 010](adr-0010-archived-cache-outcome-history.md).
