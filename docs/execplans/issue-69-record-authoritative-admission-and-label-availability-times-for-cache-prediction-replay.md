# Record authoritative admission and label-availability times for cache prediction replay

This ExecPlan records the implementation and evidence for
[issue #69](https://github.com/leynos/buildlimits/issues/69). It refines the
outcome store and recorder in
[ADR 005](../adrs/adr-0005-cache-outcome-recording.md) and the admission-time
evidence in
[ADR 011](../adrs/adr-0011-cache-outcome-admission-time-and-label-loss-accounting.md),
and it records the timing and correlation contract in
[ADR 012](../adrs/adr-0012-cache-outcome-timing-and-correlation.md). The
benchmark algorithms in
[issue #60](https://github.com/leynos/buildlimits/issues/60) are unchanged:
this is bounded instrumentation, schema and replay-integration work.

## Status

Implemented on the issue #69 branch, rebased onto the rewritten issue #68
branch. The live host service has not been upgraded; a controlled real-sccache
run is the evidence that new rows carry the correlation group, and no
historical row is retroactively repaired.

All six commit gates pass at the final commit: `check-fmt`, `lint` (ruff,
interrogate, both Pylint sweeps, Ambrleaks, Skylos, Nose duplication and the
action linters), `typecheck`, `test`, `markdownlint` and `nixie`. The opt-in
`test-sccache-host` suite runs the real observer check and the measured shell
trace against sccache 0.14.0, and it passes. One sibling latency-budget test,
which this branch does not touch, is load-sensitive. The fake-`/proc` baseline
read — a loop the test measures to calibrate itself — has alone measured a p99
of 1.98 ms, 2.02 ms and 3.02 ms against the test's 2.0 ms absolute threshold at
load averages between 13 and 22, while the whole suite passed at load average
13. When the baseline alone exceeds the budget no implementation of the code
under test could pass, so this is a host-load effect on an unrelated test, not
a regression in this change. The test remains unmodified here.

One lesson worth recording: `make test` collects the package's doctests as well
as `tests/`, so a focused `pytest tests/...` run during development does not
see an example that the format change invalidated. Examples that pin a format
version must be updated with the version, not in a follow-up.

### Resolved: reconciled with the issue #68 label-time and loss accounting

The issue #68 branch was rewritten while this branch was in review. Its new
history solves the same `observed_at` defect in its own idiom, and adds label
loss accounting this branch does not carry. The second rebase reconciled the
two designs rather than choosing one:

- The base dates every row at the statistics reading that produced it, and
  states whether that time is proof with `label_time_provenance` (`verified`)
  on a store row and `label_observed_verified` on a snapshot event. This branch
  adopted that marker and dropped its own `label_available_at` column: the two
  always carried the same value, and one column plus one statement about it
  cannot disagree with itself.
- The base's `generation_digest` names the sccache generation a reading
  observed. It is a different correlation key from this branch's
  `(supervisor_generation, attempt)` pair, which names the supervisor process
  and the admission group, and both are kept.
- What this branch adds on top is the part the base does not carry: the
  per-attempt identity, the completion moment kept apart from the label moment,
  the duplicate and conflict contract, and `predict.timing.audit` with its
  closed exclusion vocabulary and scored-versus-labelled split.
- The marker and the pair answer different questions, so verification depends
  on the marker alone. Requiring the pair as well would make every version 3 or
  4 row unscorable for a fact its release could never have written. A row that
  proves its time but names no invocation still releases; it simply takes no
  part in duplicate or conflict detection and is reported as uncorrelated.

The store format is therefore version 5 and the snapshot format version 7: the
base's version 4 fields plus this branch's correlation group, and the snapshot
additions that let a repeated attempt be checked on complete evidence. Version
1 to 4 documents still load, and their newer evidence decodes as absent rather
than guessed.

### Resolved: the third assessment round tightened the independence claim

The assessments showed that publishing an `uncorrelated` count was not the same
as keeping uncorrelated rows out of the independent-label gate, and that "same
attempt, same fingerprint" was only a duplicate when the fingerprint was
complete. Three rules changed, and ADR 012 §D4 to §D6 record them:

- `Walk.usable_labelled` now requires a complete pair and no correlation
  exclusion. A legacy row still replays and scores, but it does not count as
  one of the independent labels the minimum gate exists to require. The gate's
  diagnostic fallbacks are unchanged in meaning: when enough recorded labels
  exist but too few are usable, the verdict names the missing admission or
  availability evidence.
- `TimingAudit.uncorrelated` counts labelled, verified rows only. An `unknown`
  outcome carries no label, so counting it overstated how much of the gate
  rested on unverifiable independence.
- A repeated attempt whose copies do not both carry the evidence digest is
  ambiguous, not identical, and is excluded as a conflict. A pre-format-7
  snapshot cannot supply the digest, so re-extraction is the documented
  recovery rather than trusting a partial comparison.

Two lessons are worth recording. First, a count that is merely published does
not constrain the gate that consumes the same population; the two must be
expressed against one predicate or they drift apart silently. Second, silence
is not agreement: comparing recorded evidence has to distinguish "equal" from
"absent", or the weaker case is promoted to the stronger one by default.

The added gate pushed `predict/evaluation.py` past the 400-line module limit,
so the status decision — the counts, the closed status vocabulary and the two
predicates — moved to `predict/sufficiency.py`. The evaluation and the verdict
it assembles now read one shared decision rather than two copies of it, which
is also what the first lesson above asks for.

### Resolved: the fourth round joined the two evidence properties

The fourth assessment found the remaining composition: the gate required
independence but not a verified time, so independent-but-unverified labels
could clear the minimum while repeated legacy rows supplied the scored rows the
held-out comparison needs. The gate now counts the intersection — a complete
pair, no correlation exclusion, and a timing the audit accepts — and
`Walk.independent_labelled` keeps the independence count for diagnosis.
`insufficient label-availability times` is reported when independence is
plentiful and the withheld timing is the real shortage. Regression:
`tests/test_predict_independence.py::test_a_mixed_history_cannot_compose_one_gate_out_of_two_populations`.

The same round asked for the measured trace to start at the production
admission boundary rather than at a record the observer test builds itself.
`tests/test_sccache_outcome_shell_trace.py` now drives `mark_admission`, the
shell's launch and ending hooks, a real sccache compile, the store writer, the
store reader, the snapshot files, the audit and the replay in one opt-in run.
`tests/outcome_shell_test_support.py` gained the two injectable ports and the
supervisor handle that let it do so without duplicating the world builder.

The lesson is the same one the third round taught in a different place: two
counts that describe different properties cannot be interchanged just because
each is individually plausible. A gate has to name the conjunction it needs.

### Resolved: the fifth round carried independence into the comparison

Three gaps remained after the joint gate.

- The held-out comparison still paired every scorable row, so a run of copies
  of one verified legacy row could occupy the held-out period and decide a
  verdict. `Walk.independent` now publishes the gate's predicate per position,
  and the comparison pairs only positions it marks. Diagnostic metrics keep
  every answered row; only the decisive interval is restricted. Regression:
  `tests/test_predict_independence.py::test_legacy_copies_cannot_decide_the_held_out_comparison`.
- The two timing counts were read off the audit's first closed reason, so a row
  missing both times was excluded as `no_admission_time` and still counted as
  availability-usable. Each count now comes from the row's own evidence.
  Regression: `::test_a_row_missing_both_times_counts_as_neither_half` — five
  usable rows plus fifteen missing both times must not report twenty
  availability-usable labels.
- The measured trace wrote through the world's `FakeWriter` and serialized the
  records afterwards. It now leaves the writer to the shell, so the bytes come
  from the shell's asynchronous `StoreWriter`, and it snapshots and replays
  from that file. `tests/sccache_outcome_extraction.py` exposes `decoded_store`
  and `snapshot_of`, so both real-sccache checks share one extraction path. A
  sixth round closed the last two gaps in that work. The decision count now uses
  `admission_moment` rather than a reason comparison, and the availability
  count compares the completion moment with the verified label moment directly,
  so a row whose admission follows its own observation cannot lend a usable
  decision, and a row whose completion follows its label cannot lend a usable
  availability. The measured trace also compares each stored row with the
  snapshot event it became — attempt, generation, generation digest,
  completion, decision and label moment — and asserts the replay releases each
  label at that retained moment. Regressions:
  `::test_an_unverified_row_cannot_lend_its_unordered_decision` and
  `::test_a_late_completion_withholds_availability_despite_another_reason`.

A seventh round closed the same gap in the reporting counts. `SnapshotStats`
counted a verified availability as usable whenever the decision preceded it,
without checking the row's completion, so the dataset command and the
sufficiency block published one usable availability for a row the audit
excludes as `completion_after_availability`. The count now shares
`populations.availability_usable` with the gate, and the regression
`tests/test_predict_timing_roundtrip.py::test_a_contradictory_completion_withholds_the_reported_availability`
checks both the dataset statistics and the rendered report.

## The problem

Two independent gates must hold before the offline benchmark may score a
record: sufficient trustworthy labels, and trustworthy decision and
label-availability times. Issue #68 owns the first gate and the diagnosis of
label loss. This issue owns the second.

The version 2 store recorded the admission moment but not the moment the
observer could justify a label. `observed_at` was a completion-derived stamp:
the attribution path copied one launch's completion time onto every row of a
window, chosen from reversed launch order. The replay then treated that value
as the time the label became knowable. A label accepted at time 5 could
therefore carry a completion stamp of time 2, and a prediction admitted at time
3 could read it even though the evidence had not been accepted yet.

The store also had no identity for one invocation. A cache key recurs across
compilations, so concurrent identical invocations, retries, process-identifier
reuse and supervisor restarts were indistinguishable. Nothing in the retained
data could prove that an admission time and a label belonged to the same
attempt, and nothing could tell a retried append from a contradictory one.

## What changed

- The store format is version 5. Version 3 added `label_time_provenance`, the
  token that says whether a row's own `observed_at` is the moment its label
  became known; version 4 added `generation_digest`, the salted sccache
  generation a reading observed; version 5 adds `completed_at`, `attempt` and
  `supervisor_generation`. Versions 1 to 4 stay readable, and a field their
  release could not write decodes as absent rather than guessed.
- The supervisor marks the admission decision in its launch handler, before
  the stray-process guard or the fork. `Groups.place` only fills an entry a
  direct caller did not mark.
- The recorder driver pairs the monotonic poll with the injected UTC clock, so
  every row one accepted reading emits shares that reading's moment. Each row
  keeps its own `completed_at`; the label moment never depends on reversed
  launch order.
- A bounded `attempt` plus a per-process `supervisor_generation` identify one
  invocation, and the row's sccache `generation_digest` is compared with them.
  An exact repeated append is an idempotent duplicate; a pair that disagrees,
  including about the generation, is excluded rather than resolved. The live
  shell records each group's completion moment when it ends, so production rows
  carry `completed_at` and not only the controlled helper.
- The snapshot format is version 7 and carries every event field, including
  the generation digest and a digest over the row evidence the event does not
  map. The replay releases a label only at its verified moment, never at an
  unproven `observed_at`, and never releases, predicts or scores an unverified
  row. Equal-time batching groups the verified rows and is not split by a row
  that has no availability; the same moment places the tuning/held-out
  boundary. A snapshot that states half a correlation pair is refused.
- The snapshot loader refuses an admission-bearing history whose verified
  label times decrease in append order, and one whose label time precedes an
  admission recorded earlier in the file, on save as well as load. The checks
  are made over the rows the audit accepts, so a repeated attempt cannot fake a
  step. No monotonic absolute value is persisted; readings from unrelated boot
  epochs cannot be compared.
- `SnapshotStats`, the dataset command and the benchmark report separately:
  labelled records, withheld unverified labels, records with recorded and
  usable availability (the usable count honours the completion ordering as well
  as the decision ordering), records with usable decision timings, the scored
  population and the exclusions by closed reason, plus the duplicate, conflict
  and uncorrelated counts. The minimum-label gate counts independent labelled
  rows, so repeated appends cannot clear it, and the report states how many
  labels name no complete pair. The benchmark adds an
  `insufficient label-availability times` status beside the existing admission
  and held-out statuses.

## Acceptance evidence

The issue's regressions are covered by `tests/test_predict_timing_causal.py` and
`tests/test_predict_timing_roundtrip.py`, which use only injected clocks and
share one helper module:

| Scenario                                            | Assertion                                                            |
| --------------------------------------------------- | -------------------------------------------------------------------- |
| A completes at 2, B decides at 3, A accepted at 5   | B sees no load and no age from A                                     |
| A later decision after 5                            | sees A; a decision at exactly 5 does not                             |
| Concurrent attempts finishing out of launch order   | each row keeps its own completion and availability                   |
| Any permutation of completion order (property test) | each row's acceptance is its own and the set of moments is unchanged |
| Exact duplicate append                              | counted and excluded; it never doubles a predictor's history         |
| Contradictory attempt pair                          | both copies excluded with `conflicting_attempt`                      |
| Same attempt token in a new generation              | two distinct attempts                                                |
| Unverified row                                      | excluded with `no_availability_time`, never backdated                |
| Availability before decision                        | excluded with `admission_after_availability`                         |
| Old-format snapshot round trip                      | loads, gains no timing and is unscorable                             |
| Archive round trip                                  | version 3 fields and append order survive byte for byte              |
| Version 2 store through dataset and benchmark       | reports `insufficient label-availability times`, scores nothing      |
| Thirty-minute queue wait                            | the decision stays at the token grant, not at arrival                |
| A label accepted after its own prediction           | trains nothing and answers no later prediction                       |

The producer path is covered by `tests/test_outcome_timing_producer.py`, which
drives the supervisor's launch handler, the real reducer and the encoder. It
pins that both clocks are read at the decision before placement advances them,
that placement never re-stamps a marked entry, and that a poll without a UTC
pairing leaves the row unverified. `tests/test_outcome_timing_rows.py` covers
the row itself: that the correlation pair is written whole, that a payload
without a generation writes an explicit null rather than an invented identity,
and that encode and decode round trip. `tests/test_outcome_timing_archive.py`
pins archival preservation and the retention span.

One fixture decision is worth recording. A test that writes an older store
format by stripping fields from a current row silently stops modelling that
format once the row gains a field, and the resulting document is a hybrid no
decoder accepts. `outcome_test_helpers.older_document_row` therefore derives
each older shape from the encoder's own version table, so adding a field cannot
leave a fixture behind, and both the upgrade fixtures and the
benchmark-threshold history write their old rows through it.

### Controlled real-sccache evidence

`tests/test_sccache_outcome_observer.py` runs the real statistics and
generation ports against sccache 0.14.0 and a private cache directory. One run
of the controlled cold then warm compilation reported:

| Row       | Attempt | `admitted_at` | `completed_at` | label moment (`observed_at`) |
| --------- | ------- | ------------- | -------------- | ---------------------------- |
| cold miss | `cold`  | 07.697935Z    | 07.933240Z     | 08.708561Z                   |
| warm hit  | `warm`  | 08.710275Z    | 08.782359Z     | 09.719281Z                   |

Both rows are `solo`, carry the injected `observer-generation`, and satisfy
`admitted_at <= completed_at <= observed_at`. The label moment lands 0.78 s and
0.94 s after completion: it is the poll at which the observer accepted the
statistics reading, not the completion timestamp, and the run marks it
`verified`. That is the whole point of the change. The extraction over the same
rows reports two labels, every retained row carrying a verified availability,
and `unknown_outcome` as the only exclusion, because the mixed, timeout and
restart rows carry no hit or miss label.

This is a controlled measurement, not a live-host reading. It proves the new
rows are trustworthy; it does not claim that the live service now records them.
The live supervisor still runs the previous recorder until an operator upgrades
and restarts it.

## Limitations

- Version 1 and 2 history remains unscorable by design. No label time is
  derived from a completion stamp, a runtime, a file modification time or a
  journal match, because none of them proves when the observer accepted the
  reading.
- The live supervisor still runs the previous recorder. New rows with the
  correlation group require an operator upgrade and restart; this change does
  not modify the service.
- The availability moment is a conservative later bound: it is the poll at
  which the observer accepted the reading, not the instant the sccache child
  wrote its output. A later bound is safe for causality because it never
  releases a label early.
- sccache 0.14 still exposes no supported per-request label, so label coverage
  remains issue #68's gate and the mixed-window limitation is unchanged. This
  issue does not close #68: both evidence gates must hold there.
- Recording stays observation-only: it does not alter FIFO order, token
  accounting, queue admission, scheduling or any live prediction.

## Verification

```sh
make check-fmt
make lint
make typecheck
make test
make markdownlint
make nixie
BUILD_LIMITS_SCCACHE_HOST_CHECK=1 make test-sccache-host
```

The dataset and benchmark commands keep reporting `insufficient data`,
`insufficient admission times` or `insufficient label-availability times` until
genuine rows meet both evidence gates. Synthetic test streams are never
presented as measured host acceptance.
