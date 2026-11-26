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

### D1: version 5 rows carry the correlation group

An `outcome` row gains three fields. `attempt` and `supervisor_generation` are
one identity and are written together or not at all; `completed_at` travels
beside them and is independently optional, because a group still running when
its row was written has no completion time. A row that states half a pair is
refused rather than written, because nothing could join it to an invocation.

- `completed_at`: the UTC moment this launch's group completion was handled.
  It is retained for provenance and retention and is never a prediction or a
  release moment.
- `attempt`: the bounded admission group identifier for this one invocation.
  Production identifiers are `g<N>` within one supervisor process.
- `supervisor_generation`: a bounded random token minted once per supervisor
  process. It is the clock domain and the attempt namespace. A restart mints a
  new token, so a reused group identifier can never join a row written by an
  earlier process.

The label-availability boundary is not a separate column. It is the row's
`observed_at`, which the recorder dates at the statistics reading that produced
the label, and `label_time_provenance` states whether that time is proof. That
marker already existed in version 3; duplicating the moment would let the two
columns disagree, so they are one value and one statement about it.

### D2: the decision boundary is marked where admission is applied

The supervisor's launch handler is the first code that runs after admission
granted a token and decided to launch the group. It reads the monotonic clock
and the UTC clock together there, before the stray-process guard or the fork,
and stores them on the pending request. A direct caller of `Groups.place` that
has not marked an admission is still filled in before its fork, but an
already-marked entry is never re-stamped. Re-stamping would describe process
placement rather than the decision.

### D3: the row's moment is the reading's own, never a launch's

`PollResult` carries a UTC reading paired with its monotonic time. The recorder
driver is the only place that pairs them; the pure reducer never reads a clock.
When the reducer accepts a statistics result, every row of that window takes
the same moment: the poll boundary at which the reading returned. When it
refuses a reading, the refusal's own boundary is that moment. When a launch
overflows the window, the launch event that caused the overflow supplies it.
`_row_time` falls back to a launch's completion only for a purely synthetic
event that carries no wall clock at all.

### D4: correlation is joined by `(supervisor_generation, attempt)`

The pair is the attempt identity. An exact repeated append is an idempotent
duplicate: the first copy is scored and the later copies are counted and
excluded. An attempt that appears with different recorded evidence is a
conflict: every copy is excluded, because the document does not say which one
the observer accepted. Neither case is resolved by picking a winner.

Both tokens are bounded and validated against a path-free character set, so the
existing redaction contract holds. Neither reveals a path, an argument, a
process identifier or a host name.

The recorded evidence compared for one attempt includes the row's
`generation_digest` and a digest over the evidence the replay event does not
otherwise carry — the raw cache result, the parser class, the child sighting
and the refusal reason and attribution. Two rows naming one invocation but
different recorded evidence did not come from one reading, so they are a
contradiction rather than an idempotent retry.

A repeated attempt whose copies do not both carry the digest is *ambiguous*,
not identical. Snapshots written before format 7 hold no `record_evidence`, so
two rows that differ only in attribution would otherwise compare equal on the
fields the event does map and be accepted as one reading. Silence is not
agreement: the audit excludes every copy as a conflict, and the store is
re-extracted to recover the evidence. Only copies whose digests are present and
equal are an idempotent duplicate.

A labelled row that proved its time but names no invocation still releases: the
marker and the pair answer different questions, and requiring both would make
every version 3 or 4 row unscorable for a fact it never could have carried.
Such a row takes no part in duplicate or conflict detection, and the audit
reports it as uncorrelated rather than inventing an identity. The count covers
labelled, verified rows only — an `unknown` outcome carries no label and is not
counted. It is published beside the scored population, so a report states how
many labels rest on evidence whose independence a repeated append cannot be
checked against. A row that states one half of the pair is not this case: it
claims a correlation it cannot honour, so the snapshot loader refuses it rather
than letting it evade the correlation gate.

The minimum-label gate counts jointly usable labels: a complete attempt pair,
no correlation exclusion, *and* a timing the audit accepts. The two properties
are required together because either one alone is cheap to satisfy. A history
that is mostly retries of a few attempts cannot clear the gate on repeated
appends, a history of legacy rows cannot clear it on unrecorded independence,
and — the case that joins the two — a history cannot clear it by pairing
independent-but-unverified labels with verified-but-uncorrelated rows, since
the intersection of those populations is empty. The counts remain published for
diagnosis: the recorded-label count and the independence count say whether the
history was simply too small, and the admission-usable and availability-usable
counts, restricted to the independent labels, name which half of the timing
evidence is the binding constraint when it was not. A tie between them is not a
timing shortage, so the verdict says the labels themselves are insufficient
rather than guessing. Each timing count is derived from the row's own evidence,
never from the audit's first closed reason: a row that lacks both times is
excluded as one reason, and reading that reason as proof that the other half
was fine would count the row twice over.

The same independence requirement reaches the held-out comparison. A decisive
interval is paired only over held-out rows that name a distinct invocation, so
a run of copies of one verified legacy row cannot fill the held-out period and
stand in for evidence. Those copies still score in the diagnostic metrics,
which report every row a predictor answered; they simply cannot decide the
verdict. `Walk.independent` publishes the predicate per position, so the gate
and the comparison ask one question rather than two.

Those rows are not discarded. A legacy row whose time is verified still replays
and still scores — ADR §D4 keeps that compatibility — and a row withheld for
its timing still counts as a recorded label for diagnosis. Neither counts as
the joint evidence the gate exists to require.

### D5: the snapshot is version 7 and carries the correlation evidence

The snapshot event gains `completed_at`, `attempt`, `supervisor_generation`,
`generation_digest` and `record_evidence`, and keeps `label_observed_verified`
as the statement about its time. Version 1 to 6 documents still load, with the
newer fields absent. The replay releases a label at its verified time and
nowhere else: an unverified `observed_at` is never a release time. An event
with no verified time is not released, not predicted and not scored. The
training/held-out split uses that verified moment, and equal-time batching
groups the verified rows by it without letting a row that has none separate two
rows that share one.

### D6: chronology is checked on availability, in append order

The snapshot loader refuses a history whose verified availability times
decrease in append order: the recorder stamps each row when it accepts the
reading, so a later position carrying an earlier stamp means the clock stepped
backwards. It also refuses a verified label time that precedes a decision
recorded earlier in the file. That decision was taken before its own row was
appended, so under a monotonic clock it is strictly earlier than any later
row's reading; a step can leave the label times themselves increasing while
crossing such a decision, and only the second rule catches it. A slow
compilation appended after a faster one that was decided later is not a step.
The check is applied on write as well as on read, so the dataset command cannot
replace a readable snapshot with one its own reader refuses.

No monotonic absolute value is persisted. Monotonic readings from unrelated
boot epochs cannot be compared, so they stay inside the process where they were
taken, and the persisted UTC moments are the only cross-process ordering claim.
The supervisor generation names the process that made that claim; rows from
different generations are never joined as one attempt.

The two chronology rules are stated over the file, not over a process, so they
hold across a supervisor restart by construction. A restarted supervisor
appends after every row already in the file, and both rules compare a new row
with the rows behind it whatever generation wrote them; a new process whose
wall clock has stepped backwards therefore fails the check instead of being
excused as a fresh clock domain. A delayed delivery is stamped when the reading
is accepted, not when it was taken, so its position in the file and its stamp
move together: an ordinary delay lands later than every row already written,
and can cross neither rule. A stamp that is behind because the clock itself
stepped back is not a delay, and it is caught wherever the file can see the
contradiction — against a later verified label by the first rule, against an
earlier recorded decision by the second.

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

Availability is verified by the row's provenance marker, and the correlation
pair decides identity. `predict.events.label_moment` returns the verified
moment and nothing for an unproven row; the pair is what makes a duplicate or a
conflict, and two rows join only within one generation. Keeping the two rules
apart is what lets an older verified row still score while an uncorrelated row
is reported as uncorrelated.

## Consequences

- A controlled cold/warm run through the real sccache observer now produces
  rows whose label moment is the reading that accepted it and whose
  `admitted_at` precedes it, with the completion moment beside them. The
  extraction reports a scored population that is smaller than its labelled
  population, and says why.
- Version 1 and 2 history remains readable and contributes no labels to the
  benchmark. An operator who wants usable evidence must run a recorder that
  writes the provenance marker; no historical row is retroactively repaired.
- A row grows by the three correlation fields, up to about 140 bytes for the
  longest attempt and generation tokens. Segment boundaries and archive sizes
  move again; the retention and archive tests use the encoder's own size rather
  than a hard-coded row size.
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
