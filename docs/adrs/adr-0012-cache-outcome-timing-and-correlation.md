# Architectural decision record (ADR) 012: cache-outcome timing and correlation

## Status

Proposed. The decision is implemented on the issue #69 branch; accept this
record when that work merges. It refines the outcome store and recorder in
[ADR 005](adr-0005-cache-outcome-recording.md) and the admission-time evidence
in
[ADR 011](adr-0011-cache-outcome-admission-time-and-label-loss-accounting.md).

## Date

2026-11-24.

## Context and problem statement

The offline prediction benchmark needs to know two things about every label it
scores: when the supervisor decided to admit the compilation, and when the
observer could actually justify the label. Version 2 of the store recorded the
first and not the second. Its `observed_at` field was taken from the moment a
group's completion was handled, and the attribution path copied one launch's
value onto every row of the window. That is neither the completion of the
statistics query that produced the attribution nor necessarily the latest
completion in the window; it is simply the last value the recorder happened to
iterate over.

Treating that field as label availability is unsafe. A label whose reading was
accepted at time 5 could carry a completion stamp of time 2. A prediction
admitted at time 3 would then see the label as already available, even though
the observer had not yet accepted the evidence that produced it. The reverse
error is also possible: a label could be withheld from a prediction that could
legitimately have used it.

The store also had no per-invocation identity. A cache key recurs across
compilations, so two concurrent identical invocations, a retried compilation,
process-identifier reuse or a supervisor restart all look alike. Nothing in the
retained data let a reader prove that the admission time and the label belonged
to the same attempt, and nothing let a duplicate append be told from a
conflicting one.

## Decision drivers

- Never backdate a label. A completion time, a runtime and a row's write time
  are all weaker evidence than the moment the observer accepted the reading.
- Keep the prediction boundary explicit. Admission, process launch, completion
  and label availability are four different moments; a future change in where
  predictions affect policy must not reuse one timestamp as another.
- Preserve history. Version 1 and 2 rows stay readable and keep their original
  provenance; they are not rewritten into a shape they never had.
- Make gaps visible. A report must state how many records carry a trustworthy
  decision time, how many carry a trustworthy availability time, how many are
  usable, and why the rest are excluded.
- Keep the recording bounded and observation-only. Nothing here may alter FIFO
  order, token accounting, queue admission, scheduling or a live predictor.

## Decision

### D1: version 3 rows carry the complete timing group

An `outcome` row gains four fields. They are written together or not at all;
the encoder writes the version 2 shape when the group is incomplete, so a row
can never claim evidence it does not have.

- `completed_at`: the UTC moment this launch's group completion was handled.
  It is retained for provenance and retention and is never a prediction or a
  release moment.
- `label_available_at`: the UTC moment the observer accepted the reading that
  justifies the label. For an accepted attribution this is the poll boundary at
  which the statistics result and the server generation were both observed. For
  a refusal it is the moment the refusal was decided. It is never the group's
  completion time.
- `attempt`: the bounded admission group identifier for this one invocation.
  Production identifiers are `g<N>` within one supervisor process.
- `supervisor_generation`: a bounded random token minted once per supervisor
  process. It is the clock domain and the attempt namespace. A restart mints a
  new token, so a reused group identifier can never join a row written by an
  earlier process.

`observed_at` keeps its historical meaning. A version 3 row sets it to its own
group's completion-handling time when one was observed, else to the moment the
row's outcome was decided. The recorder no longer selects one launch's
timestamp for a whole window.

### D2: the decision boundary is marked where admission is applied

The supervisor's launch handler is the first code that runs after admission
granted a token and decided to launch the group. It reads the monotonic clock
and the UTC clock together there, before the stray-process guard or the fork,
and stores them on the pending request. A direct caller of `Groups.place` that
has not marked an admission is still filled in before its fork, but an
already-marked entry is never re-stamped. Re-stamping would describe process
placement rather than the decision.

### D3: availability is captured at the poll boundary

`PollResult` carries a UTC reading paired with its monotonic time. The recorder
driver is the only place that pairs them; the pure reducer never reads a clock.
When the reducer accepts a statistics result, every row of that window takes
the same availability moment. When it refuses a reading, the refusal's own
boundary is the row's availability. When a launch overflows the window, the
launch event that caused the overflow supplies the moment.

### D4: correlation is joined by `(supervisor_generation, attempt)`

The pair is the attempt identity. An exact repeated append is an idempotent
duplicate: the first copy is scored and the later copies are counted and
excluded. An attempt that appears with different recorded evidence is a
conflict: every copy is excluded, because the document does not say which one
the observer accepted. Neither case is resolved by picking a winner.

Both tokens are bounded and validated against a path-free character set, so the
existing redaction contract holds. Neither reveals a path, an argument, a
process identifier or a host name.

### D5: the snapshot is version 3 and the replay uses verified availability

The snapshot event gains `completed_at`, `label_available_at`, `attempt` and
`supervisor_generation`. Version 1 and 2 documents still load, with the new
fields absent. The replay releases a label at its verified availability time
and nowhere else: `observed_at` is never a release time. An event with no
verified availability is not released, not predicted and not scored. The
training/held-out split and the equal-time batching both use availability, so
rows accepted at one reading land in one batch.

### D6: chronology is checked on availability, in append order

The snapshot loader refuses a history whose verified availability times
decrease in append order: the recorder stamps each row when it accepts the
reading, so a later position carrying an earlier stamp means the clock stepped
backwards. Only two verified times form a pair; a slow compilation written
after a faster one that started later is not a step. The check is applied on
write as well as on read, so the dataset command cannot replace a readable
snapshot with one its own reader refuses.

No monotonic absolute value is persisted. Monotonic readings from unrelated
boot epochs cannot be compared, so they stay inside the process where they were
taken, and the persisted UTC moments are the only cross-process ordering claim.
The supervisor generation names the process that made that claim; rows from
different generations are never joined as one attempt.

### D7: reports separate the two evidence gates

`SnapshotStats` counts labelled records, records with a recorded decision time,
records with a usable decision time, records with recorded and usable
availability, usable scored records, and exclusions by closed reason
(`unknown_outcome`, `no_admission_time`, `no_availability_time`,
`admission_after_availability`, `completion_after_availability`,
`duplicate_attempt`, `conflicting_attempt`). The dataset command and the
benchmark's sufficiency block both print those counts. The benchmark adds an
`insufficient label-availability times` status distinct from
`insufficient admission times`, and the verdict keeps the existing
strict-before rule: a decision at exactly the availability instant does not see
that label.

Availability is verified as a whole group. A row that names an availability
moment without its attempt and generation cannot be correlated to one
invocation, so `predict.events.label_moment` returns nothing for it and it
releases no label. The same rule decides what a duplicate or a conflict is: two
rows join only within one generation.

## Consequences

- A controlled cold/warm run through the real sccache observer now produces
  rows whose `label_available_at` is later than `completed_at` and whose
  `admitted_at` precedes both. The extraction reports a scored population that
  is smaller than its labelled population, and says why.
- Version 1 and 2 history remains readable and contributes no labels to the
  benchmark. An operator who wants usable evidence must run the version 3
  recorder; no historical row is retroactively repaired.
- A row grows by the four timing fields, up to about 140 bytes for the longest
  attempt and generation tokens. Segment boundaries and archive sizes move
  again; the retention and archive tests use the encoder's own size rather than
  a hard-coded row size.
- A duplicate append is no longer double-counted in a predictor's history. A
  conflicting pair is visibly excluded rather than silently scored twice.
- The store's append order is still the only record order; nothing sorts by a
  timestamp. Archival copies original row bytes, so timing, correlation and
  order survive a round trip.

## Alternatives considered

- **Reuse `observed_at` as availability.** Rejected: it is a completion-derived
  timestamp the observer did not accept. Reinterpreting old rows would
  retroactively bless evidence nobody recorded.
- **Derive availability as `observed_at + a fixed bound`.** Rejected: a bound
  invented from the sampler's cadence is not a recorded moment, and it would
  vary by configuration.
- **Derive availability from the row's write time or the journal receive
  time.** Rejected: the writer is asynchronous, and the journal carries no
  per-compilation identity. Both are later, unrelated observations.
- **Use the cache key as the attempt identity.** Rejected: identical concurrent
  invocations, retries and restarts share it.
- **Use the process identifier and start time.** Rejected: it is a host detail,
  it can be reused, and it is not bounded in the same privacy-safe way.
- **Split a conflicting duplicate by keeping the first.** Rejected: nothing in
  the document says which copy the observer accepted, so neither is usable.
- **Refuse a history that spans supervisor generations.** Rejected: a restart
  does not itself make the UTC moments incomparable; the generation is there to
  keep attempt identifiers apart, and the availability-order check catches a
  clock step that would matter.
- **Persist monotonic values.** Rejected: values from unrelated boot epochs
  cannot be compared, and no contract would make them safe to join.

## Follow-up

- Label coverage, attribution-loss diagnosis and the combined data-sufficiency
  acceptance remain with
  [issue #68](https://github.com/leynos/buildlimits/issues/68). This decision
  supplies the timing and correlation contract those labels need; it does not
  by itself close that issue.
- Predictor research stays with
  [issue #60](https://github.com/leynos/buildlimits/issues/60) and the merged
  benchmark in [PR #61](https://github.com/leynos/buildlimits/pull/61). No
  predictor is changed here.
- Retention [#64](https://github.com/leynos/buildlimits/issues/64) and archival
  [#63](https://github.com/leynos/buildlimits/issues/63) already preserve
  record order and original row bytes; their next rollout must keep accepting
  version 3 rows unchanged.
