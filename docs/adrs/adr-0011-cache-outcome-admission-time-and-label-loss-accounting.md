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
even establish a first baseline while the host was busy, because the queue
clause was applied to a baseline as well as to an attributing reading.

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
writer that compacts an old store writes a current header while preserving the
old rows, so a version 2 file may contain version 1 rows; the decoder validates
each row's shape, not a header-wide assumption, and the shape it accepts is
fixed by the document's own version. The snapshot gains this evidence at
version 2, and D6 raises it to version 3; a version 1 snapshot loads with
diagnostics unavailable. D9 raises the store to version 3 and fixes that row
shape by version.

### D2: keep label-loss accounting in the snapshot, not in per-request logs

The snapshot carries one bounded `diagnostics` block. Its retained-store part
counts outcome rows by cache result, outcome reason, attribution, origin,
parser class, child sighting and sccache version, plus recorded and missing
admission times and the labelled total. Its history part names the archived and
sealed segments, the missing sequences, the quarantined file count, the
retention floor and whether the gap list was truncated. The gap count is
measured against the retention floor the archiver recorded, so a prefix
retention deleted is not reported as loss; a floor that cannot be read
withdraws the gap and quarantine counts as unavailable rather than presenting
them as measured.

The dataset command also records the observer's own counts from the user
journal: admitted requests by cache path and backend, published outcomes by
result, reason, attribution and origin, the writer's appended and dropped or
evicted rows split by the closed `StoreReason`, completed, skipped and failed
readings, and the gate reasons the journal witnessed. The four denominators
stay distinct: all admitted requests, cache-eligible compilations, retained
`OutcomeRecord`s and scored outcomes. A report states each one rather than
dividing by whichever is convenient.

The `StoreReason` split is what separates retention loss from a failed write:
`expired` names a row retention removed, while `save_failed`, `seal_failed`,
`corrupt` and `version_mismatch` name a write the store could not keep. Only a
writer result that actually discarded a row contributes to the map, so its
counts sum to the discarded total instead of including the reason of every
clean append.

Issue #68 also asks for label loss by sccache generation. A store row's key
carries the server *version*, not the generation the recorder observed when it
read a window, so this dimension cannot be measured from retained history and
is not given a new row field: no report could reconcile a field the existing
rows do not carry. Both the dataset report and the rendered Markdown name it
`by generation: unavailable` with that reason, which is bounded and honest
rather than omitted or filled with a count.

The snapshot loader reconciles the diagnostics before a report can print them:
every breakdown must sum to the outcome rows, the record totals must add up,
the admission counts must cover the same rows, the labelled total must equal
the hit plus miss counts, the observer's store reasons must sum to its
discarded total, and the event rows must agree with the block's per-label,
per-origin and admission counts. Changing one event's origin changes what the
Markdown origin table reports about the same history, so it is a join failure
rather than a harmless edit.

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

### D4: a label is dated when its reading returned

`observed_at` is the moment the label or refusal became known, and nothing
else. The recorder takes that wall-clock moment from the event that caused the
emission:

- `PollResult` carries it for an attributed, refused or straddled window: the
  statistics query has returned, so its labels can now be read.
- `Launched` carries it for a window that overflows, because the refusal is
  known the moment the overflowing launch arrives.
- `Stopping` carries it for the launches a supervisor stop leaves unattributed.

Every row of one window, including its `WindowSummary`, shares that single
moment. A launch's own completion time is still recorded (`Ended`, and the
retained launch's `completed_at`) as evidence, and is used only as the
documented fallback for a purely synthetic event that carries no wall clock at
all. The supervisor shell stamps every launch, gate, poll and stop, so no real
row takes that fallback.

Two consequences follow. First, a prediction admitted between a launch's
completion and the reading that labels it can no longer see that label, so the
walk-forward schedule cannot leak it backwards. Second, two launches finishing
out of order no longer produce descending observation times in store order:
they share the reading's moment, so the snapshot loader's backwards-clock
refusal fires only on a genuine wall-clock step.

Rows written by an earlier build of this branch carry the old, launch-derived
`observed_at`. They remain readable, but their recorded time may precede the
label's true knowledge time, and the same store format version cannot separate
them from corrected rows. No timestamp field was invented to mark them. D6
records how the snapshot and benchmark handle that residual risk.

### D5: an older active file is settled before the writer appends

`StoreHeader` carries the `version` the document was read at or will be written
with, defaulting to `FORMAT_VERSION`; decoding preserves the file's own value.
On a segmented restart, a loaded header older than the current format seals the
old active file through the existing sealing path, which closes the old
document intact and installs a fresh current-format header. A compaction or a
seal always writes the current version. The legacy single-file path rewrites
the document in place through compaction, which also installs a current header;
a compaction or seal that fails clears readiness instead of allowing an append,
so no current-format row is ever written beneath an older header.

This is what lets a downgraded reader classify the store correctly. Before the
change, a version 1 header could stay in place while version 2 rows were
appended beneath it, so a version 1 reader decoded the header happily and then
reported the first new row as `corrupt`. After the change, the only header the
writer leaves in place is current, so that reader reports `version_mismatch` at
the header before it reads any row.

### D6: a snapshot event carries whether its label time is proven

The snapshot format becomes version 3, and each event carries an explicit
`label_observed_verified` boolean. The marker is per event rather than a
snapshot-wide `label_time_provenance` name, because the property is a fact
about one row, not about a document: a snapshot built from a store that later
gains a provenance-carrying row format would otherwise have to claim one level
of proof for every row it holds. A per-row field composes with that future
without a second format change, and the reconciliation checks compare like with
like.

The snapshot writer always emits the marker for the rows it derives now. The
dataset command cannot distinguish a pre-fix version 2 row from a fixed version
2 row — both are version 2 store rows — so it emits them **unverified**.
Claiming verification for a version 2 row would silently release an old label
before the reading that produced it, which is exactly the leak D4 closed. A
version 3 store row that carries its own provenance marker, as D9 specifies, is
the first source that can emit a verified event.

The loader treats a version 1 or 2 snapshot's events as unverified, and refuses
a version 3 event that omits the marker or sets it to a non-boolean. Absence is
therefore never read as proof.

An unverified hit or miss is neither released nor scored: `scored_label` returns
``None`` for it, so training, scoring and the phase schedule cannot disagree
about which labels are usable. The replay reports the count as
`unverified_label_time`, and both the plain dataset report and the rendered
Markdown name it, so a withdrawn history is distinguishable from one that
recorded few labels. A source that can prove its label times, such as the
benchmark's own known-property synthetic streams, states
`label_observed_verified=True`.

### D7: an outcome that produced no store row is counted by cause

A cached launch with no cache key or no wall-clock label moment publishes an
outcome event but produces no `OutcomeRecord`. The store writer never sees it,
so its discard counts can never account for it and the difference between
published outcomes and stored rows stayed unexplained. The recorder now
computes one closed `RecordOmission` reason per launch and copies it onto the
event it publishes: `key_unavailable`, `label_time_unavailable` or `none`. The
event and any row for it are built from the same launch in one transition, so
the token describes what happened to that launch and is not a second guess made
afterwards.

`ObserverAccounting.record_omitted` sums those reasons, and
`by_omission_reason` breaks them down. The journal codec requires the map to
sum to the total. `record_omitted` is a token on the event, not a counter: one
event describes one outcome, so the observer derives the count from the closed
values instead of the recorder publishing a tally beside them.

Those counts name the categories a published outcome can fall into, and nothing
stronger. A published outcome is an appended outcome row, a pre-store omission,
a discarded write or an outstanding queued write. The report does not prove
that identity, because it cannot correlate the terms. `store_records` counts
every appended row, including a reading's window summary; `store_discarded`
counts evictions as well as failed writes; and a write still queued when the
window closed is not measured at all. Subtracting the observed totals from
`outcomes` therefore does not account for each outcome once, and a report that
claimed it did would assert a reconciliation its instrumentation cannot
demonstrate.

A journal record that carries no omission field, as an older recorder wrote it,
is counted in `record_omitted_unrecorded` rather than contributing zero to
either total, so absence is not read as a measured `none`. The journal codec
requires the omitted and unrecorded totals together to fit inside the outcomes
the window held.

### D8: the store total counts appends, not every operation's rows

`store_records` was summed across every `outcome_store` operation. A compaction
reports the rows it retained and a seal the rows it released, but neither wrote
a row the store did not already hold, so the total counted some rows several
times while reports called it "store appends". Only `operation == "append"`
contributes to the append total now; every result still appears in
`by_store_operation`, so the compactions and seals are visible beside the
appends. Both the dataset report and the rendered Markdown label the number
"rows appended" and state the rule, so no report calls a mixed total appends.

### D9: a store row states its own label-time provenance

D6 withdrew every store-derived label because a version 2 row cannot say which
code dated it. That was correct for history already written, but it left no
path by which a correctly recorded row could ever be scored: the dataset
adapter marked every store-derived event unverified, whatever wrote it.

The store format becomes version 3, and an `outcome` row gains
`label_time_provenance`, written `verified` by the recorder that dates a row at
its statistics reading. The marker is per row rather than per document, for the
same reason D6 gives for the snapshot marker: one snapshot may hold rows of
mixed provenance, and a document-wide claim would have to overstate one of
them. A version 1 or 2 row decodes the field as `None`, which means unproven
rather than false, and the adapter maps `None` to an unverified event, so older
history keeps exactly the treatment D6 gave it. No historical row is repaired
or re-stamped.

The accepted row shape is fixed by the document's own version, not by the
running code. A version 3 row that omits the marker is corrupt, because the
version 3 shape is exact: a row preserved from an older document is re-encoded
by compaction with the field present and null, so no current document holds a
row that predates the marker. `encode_line` takes the document version for the
same reason and refuses to write a verified row into an older document, whose
shape has nowhere to state it. Every reader that decodes rows beneath a header
passes that header's version on, so an older document stays readable while a
current row that omits its marker is refused.

The recorder writes `verified` only when the emission's own moment dated the
row. The completion-time fallback exists for a purely synthetic event carrying
no wall clock; a completion time is a lower bound on when the label became
known, so a row dated that way states no provenance rather than claiming proof
the shell never supplied.

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
- Version 2 rows written before this decision's label-time fix remain readable
  but are not scorable through the benchmark: the dataset command emits their
  label times as unverified, so the replay withholds them and reports the
  count. No historical row is repaired, and no report claims a version 2 row
  proves when its label was known; see D4 and D6. A history recorded by a
  version 3 store is scorable, because its rows state their provenance; an
  operator who needs one must run the current recorder and let new rows
  accumulate, since no older row can be promoted.
- The snapshot format is version 3. A version 1 or 2 snapshot still loads, with
  its events unverified; a version 3 event that omits the marker is refused.
- A published outcome that produced no store row is counted by its closed
  reason, and an event whose omission evidence is absent is counted as
  unrecorded rather than as a measured `none`; see D7. The report names the
  four categories a published outcome can fall into and states that it does not
  prove that identity, because the append, discard and queued terms cannot be
  correlated with it.
- A version 3 store row states its own label-time provenance, so a history the
  current recorder wrote is scored while version 1 and 2 rows stay withheld;
  see D9. The store row grows again, so segment boundaries and archive sizes
  move once more, and the tests that pinned a literal row size derive it from
  the row instead.
- The store append total counts only `append` results, and the operations that
  retained or released rows appear in their own map; see D8. A reader who
  previously summed every operation's `store_records` sees a smaller, correct
  append total.
- A restart settles an active file written under an older header before the
  first append. In segmented mode this costs one extra closed segment and one
  sequence number; no row is dropped. The store row grows by about 71 bytes, so
  segment boundaries and archive sizes move. Archive tests and byte budgets
  that assumed the version 1 row size are updated.
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
- **Add a server-generation field to the store row.** Rejected: a row's key
  already names the server version, and no report could reconcile a new
  generation field with the rows already written, so the dimension is reported
  as unavailable instead of adding a field that only future rows would carry.
- **Report a gap count from an unreadable retention floor.** Rejected: without
  the floor, an absent sequence below the survivors cannot be told apart from a
  sequence retention removed on purpose, so the counts are withdrawn rather
  than printed as loss.
- **Keep releasing labels at a version 2 row's recorded time.** Rejected: a
  version 2 row may carry a launch-derived time that precedes the reading that
  produced it, and the format version cannot separate those rows from corrected
  ones. Releasing at the recorded time would score a prediction on a label it
  could not have seen at admission; the label is withheld and counted instead.
- **Mark verification once per snapshot rather than per event.** Rejected: the
  fact is a property of one row's provenance, and a snapshot-wide flag would
  force one answer onto rows of mixed provenance, which a provenance-carrying
  store row format would immediately expose.
- **Attribute the missing-outcome difference to an unmeasured residual.**
  Rejected: the recorder knows why it did not key a launch, so the reason is
  recorded on the event rather than left as a hypothesis over two totals the
  reader cannot correlate. Recording the reason does not close the arithmetic
  either; it names one category, not a proof of identity.
- **Call the four categories a proved reconciliation.** Rejected: the appended
  total carries a reading's window summary, the discarded total carries
  evictions as well as failed writes, and an outstanding queued write is not
  counted at all, so no subtraction of the published totals accounts for each
  outcome once. The report names the categories and says plainly that it does
  not prove the identity.

## Follow-up

- The benchmark continues to report `insufficient data` or
  `insufficient admission times` until genuine rows meet both gates. A history
  whose labels are all unverified reports `insufficient data` with the withheld
  count, because an unproven label time is not scored evidence.
- A version 3 store row records the provenance of its own label time and emits a
  verified event through `event_of`; that adapter is the one place D9's
  decision lives, so a later source format changes it once.
- A later observation port may add a supported per-request backend signal for a
  backend that provides one. It must keep the mixed-window `unknown` result for
  sccache 0.14.
- Permanent host provisioning and the timer that archives history remain with
  [dev-env-rocky #424](https://github.com/leynos/dev-env-rocky/issues/424) and
  the archival work in [ADR 010](adr-0010-archived-cache-outcome-history.md).
- **Blocking follow-up: per-generation label accounting.** Issue #68 also asks
  for label loss by sccache generation, and this decision records that
  dimension as unavailable rather than measuring it. The concrete design that
  would close it, kept blocked until a store format can carry it:
  - A version 4 store row gains `generation_digest: str | None`: a salted
    HMAC-SHA-256 over the recorder's `(invocation, pid, starttime)` identity,
    using the same per-store salt domain as the cache key, so no host path or
    process id is retained.
  - The recorder writes it from the reading's observed boundary generation on
    every row an attributing or refusing reading emits; version 1 to 3 rows
    decode it as absent. Because a server can change inside a reading, the
    boundary generation is an *observation of the window's end*, not proof of
    which server handled each compilation: `generation_changed` windows write
    null, since no single generation can be attributed to their rows.
  - `StoreAccounting` gains a bounded `by_generation` map (the same 64-entry
    cap as the other dimensions, with `unrecorded` for older rows) and the
    snapshot diagnostics gains the matching block, which the loader reconciles
    against the outcome-row total exactly as it does the other breakdowns.
  - The correlation is the digest itself: it joins each label to the server
    generation that produced it, so `generation_changed` loss can be separated
    per generation instead of appearing only as one refusal reason.
  This blocks the generation dimension of the issue, not the label-coverage or
  admission-time work, which stand on their own.
