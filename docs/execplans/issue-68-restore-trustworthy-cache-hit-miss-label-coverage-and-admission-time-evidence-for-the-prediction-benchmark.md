# Restore trustworthy cache hit/miss label coverage and admission-time evidence

This ExecPlan records the implementation and host evidence for
[issue #68](https://github.com/leynos/buildlimits/issues/68). It refines the
outcome store and recorder in
[ADR 005](../adrs/adr-0005-cache-outcome-recording.md) and is governed by
[ADR 011](../adrs/adr-0011-cache-outcome-admission-time-and-label-loss-accounting.md).
The benchmark algorithms in
[issue #60](https://github.com/leynos/buildlimits/issues/60) are unchanged.

## Status

Implemented on the issue branch, including the review follow-ups recorded in
[Label timing and the store-format upgrade](#label-timing-and-the-store-format-upgrade),
in
[Review follow-up: inventory, quarantine and report honesty](#review-follow-up-inventory-quarantine-and-report-honesty),
in
[Review follow-up: label-time provenance, omitted outcomes and append totals](#review-follow-up-label-time-provenance-omitted-outcomes-and-append-totals)
and in
[Review follow-up: the store states its own provenance](#review-follow-up-the-store-states-its-own-provenance)
below. The live host service has not been upgraded: the before/after report
below distinguishes the old recorder's retained history from the new code's
controlled evidence, and says plainly where an operator action is still
required.

## The problem, measured

[PR #61](https://github.com/leynos/buildlimits/pull/61) reported 10 labels from
10,159 cache-outcome events, a labelled share of about 0.10% against the
benchmark's 2,000-label gate. The benchmark reported `insufficient data`, which
was correct. The issue asked for the upstream observation path to be measured
and repaired, not for the gate to be weakened.

### Before: the live host at 2026-11-23T21:08Z

A read-only copy of `~/.local/state/buildlimits/cache-outcomes.jsonl` held:

| Measure                                | Observed               |
| -------------------------------------- | ---------------------- |
| Outcome rows                           | 15,846                 |
| Window summaries                       | 0                      |
| Attributed `hit`/`miss` labels         | 0                      |
| Distinct hashed identities             | 13,412                 |
| Admission times on rows                | 0                      |
| Parser class `cacheable`               | 4,420                  |
| Parser class `declined`                | 11,426                 |
| Origin `registry` / `path` / `unknown` | 12,996 / 2,680 / 170   |
| Observation span                       | 19:00:29Z to 20:08:53Z |

The journal over a fifteen-minute window of the same sustained load held:

| Measure                           | Observed      |
| --------------------------------- | ------------- |
| Admitted requests                 | 3,801         |
| Admitted through the cache path   | 3,786 (99.6%) |
| `cache_outcome` events            | 3,835         |
| Events labelled `window_overflow` | 3,835 (100%)  |
| Completed statistics readings     | 0             |
| Reading ticks skipped             | 839           |
| Writer appends reported           | 3,480         |
| Rows dropped or evicted           | 0             |

A wider journal read over the whole observation span found 36,530
`cache_outcome` events, every one `window_overflow` at 65 groups. That is
exactly 562 full-window flushes. The same read reported 36,541 cached
admissions and 28,030 rows dropped or evicted by compaction.

### Diagnosis

The recorder could open a statistics read only when `queued == 0` and
`cached_moving == 0`. The host queue stayed several hundred requests deep, so
the queue clause never held. The bounded window then filled to 64 entries and
flushed every launch as unattributed. The flush kept the old statistics
baseline, so the overflowed requests remained in sccache's cumulative counters
and the next window could not balance either.

An early revision also relaxed `cached_moving == 0` and `server_drain == 0` for
the first baseline, on the reasoning that a baseline only snapshots the
counters. That is unsound with aggregate counters: a compilation still in
flight when the baseline is read contributes its remaining counters to the next
window's delta while belonging to no window. Review rejected the relaxation,
and the movement clauses now hold for every reading. Only the queue clause is
removed, because the straddle rule already refuses a launch that starts during
the query.

The second gap was independent: `OutcomeRecord` had a label time and a runtime
but no admission time, so the benchmark could not prove that a prediction used
only information available before admission.

## What changed

- The store format is version 3. An outcome row carries `admitted_at` (UTC,
  captured where admission is decided), `outcome_reason`, `outcome_attribution`
  and `label_time_provenance`. Version 1 rows stay readable and decode those
  fields as `None`; a version 2 row decodes the provenance marker as `None`.
  Both are emitted unverified and never scored.
- The snapshot carries bounded diagnostics: retained rows by result, reason,
  attribution, origin, parser class, child sighting and sccache version;
  admission-time coverage; the eligible cacheable denominator; and closed
  history loss. The snapshot format is version 3, and every event states
  `label_observed_verified`, so an unverified label time is neither released
  nor scored (ADR 011 D6).
- The dataset command records the observer's own journal counts, including
  admitted requests by cache path and backend, published outcomes by result and
  reason, writer appends and evictions split by the closed `StoreReason`, and
  reading skips and failures. The reason split is what separates a row
  retention expired from one a failed write dropped.
- Label loss by sccache generation is reported as `by generation: unavailable`
  with its reason. A store row's key carries the server version, not the
  generation the recorder observed, and no v3 row field was added, because a
  field only future rows carry could not be reconciled with retained history.
- Closed history is measured against the recorded retention floor, so a prefix
  retention deleted is not reported as a gap. A floor that cannot be read
  withdraws the gap and quarantine counts as unavailable, and the quarantine
  count is measured from its directory rather than reported as an always-empty
  zero.
- The quiet-point gate no longer requires an empty queue. Every reading,
  baseline included, still requires no cached group to be moving and no
  detached work to be draining, so a delta only ever contains launches its
  window holds.
- A window overflow now resets the baseline, so the overflowed requests cannot
  poison later windows.

The approved attribution rules are unchanged: a solo or unanimous window
produces a label, and a mixed, unbalanced, ineligible or straddled window stays
`unknown` with its reason.

## Label timing and the store-format upgrade

Two review findings changed the recorder and the store restart. Both are
recorded as decisions D4 and D5 in
[ADR 011](../adrs/adr-0011-cache-outcome-admission-time-and-label-loss-accounting.md).

### A label is dated when its reading returned

`observed_at` was stamped when the process group emptied, then copied onto
every row the later statistics reading emitted. sccache exposes no label until
that reading returns, so the recorded time could precede the moment the label
was really known. `admission_schedule` releases labels at the recorded time, so
a prediction admitted in between could read a label that had not been observed
yet. The same per-launch stamps, written in launch order, let two launches
finishing out of order produce decreasing observation times, which the snapshot
loader refuses as a backwards clock.

The recorder now takes the observation time from the event that caused the
emission: the `PollResult` whose query returned for attributed, refused and
straddled windows, the overflowing `Launched` event for a window overflow, and
the `Stopping` event at shutdown. Every row of one window, its summary
included, shares that moment. The launch's own completion time remains on
`Ended` as evidence and is used only by the documented fallback for a purely
synthetic event that carries no wall clock.

New unit evidence covers both defects. A delayed-query test proves a label is
not released before its query returned, and a reverse-completion test proves
rows from two launches finishing out of order share one reading moment, load
through the snapshot, and remain scorable.
`tests/test_predict_snapshot_clock.py` adds the same statement at the snapshot
boundary: two rows sharing one reading moment load and score instead of being
refused, while a genuine backward step is still refused.

Rows already written with a version 2 header by the earlier build keep their
launch-derived `observed_at`. They stay readable, but their recorded time may
precede the true knowledge time and the format version cannot identify them.
The later review round below tightened this: rather than releasing those labels
at a recorded time that is only a lower bound, the dataset command marks every
version 2 row's label time unverified and the benchmark withholds it (ADR 011
D6).

### An older header is settled before the writer appends

`settle_loaded_store` left a same-salt, non-expired, below-limit segmented
active file in place. After an upgrade, the writer appended version 2 rows
beneath its version 1 header; a version 1 reader then reported the new row as
`corrupt` rather than the file as a `version_mismatch`.

`StoreHeader` now carries the format version its document was read at or will
be written with, and decoding preserves the file's own value. A restart whose
loaded header is older than `FORMAT_VERSION` seals the active file through the
existing sealing path in segmented mode (or compacts it in place on the legacy
path), and both transitions install a current header. A failed seal or
compaction clears readiness rather than appending. Migration behaviour is in
[the migration guide](../migrating-to-cache-outcome-recording.md).

`tests/test_outcome_store_upgrade.py` pins the behaviour: a small, unexpired,
same-salt version 1 file is sealed on a segmented restart and no version 1
header remains above the appended row; a current reader decodes the upgraded
file; a version 1 decoder reports `version_mismatch` rather than `corrupt`; and
the legacy path rewrites the header in place. `tests/segment_test_helpers.py`
gained `version_one_document`, which reproduces the bytes an older release
really wrote, because a version 2 encoder cannot emit them.

## Controlled evidence

The opt-in check `tests/test_sccache_outcome_observer.py` drives the real
statistics and generation ports against sccache 0.14 and a private cache
directory. One run produced:

| Step                               | Observed result                                  |
| ---------------------------------- | ------------------------------------------------ |
| First baseline                     | accepted, no label                               |
| Cold compile                       | `miss`, `solo`, admission at or before the label |
| Warm compile, same metadata        | `hit`, same key                                  |
| Cold miss and warm hit, concurrent | two `unattributed` rows, `mixed_window`          |
| `rustc -vV` probe                  | `declined`, `solo`                               |
| Statistics deadline                | `unattributed` with `timeout`                    |
| Generation change inside the query | `unattributed` with `generation_changed`         |

The extraction over those rows reported two labels, every retained outcome
carrying a usable admission time, and the mixed, timeout and restart reasons
counted separately.

Unit and property tests cover the closed reason breakdowns, the reconciliation
of every denominator, version 1 and 2 compatibility, the admission-time round
trip, and the gate changes. A verified synthetic snapshot can pass both gates,
but admission coverage alone does not make a store history scorable: the rows
must also prove their label-time provenance. A version 1 or 2 store row carries
no admission time and no proof, so it predicts nothing and its labels are
withheld; only a version 3 row, written by the fixed recorder, carries both.

## After: the same host data, extracted by the new code

The new dataset command reads the copied version 1 store and reports:

```text
events=15846 labelled=0 identities=13412 gaps=0
admission times: recorded=0 usable=0 missing=15846 after their own label=0
store rows: total=15846 outcomes=15846 summaries=0 labels=0
labelled fraction=0.0000 of outcome rows; cacheable rows=4420 with 0 labels
by cache result: unattributed=15,846
by outcome reason: unrecorded=15,846
by attribution: unrecorded=15,846
by origin: path=2,680, registry=12,996, unknown=170
by parser class: cacheable=4,420, declined=11,426
by server version: 0.14.0=15,846
```

This is the expected result, not a new failure. The rows predate the fields, so
their reason and attribution are `unrecorded` and their admission time is
absent. The extraction now states that explicitly instead of presenting one
undifferentiated `unknown` bucket. Historic rows are not relabelled.

The same command was run again over the live store after the final review
round, at 2026-11-24T02:40Z:

```text
events=12209 labelled=0 identities=10821 gaps=0
admission times: recorded=0 usable=0 missing=12209 after their own label=0
store rows: total=12209 outcomes=12209 summaries=0 labels=0
labelled fraction=0.0000 of outcome rows; cacheable rows=3825 with 0 labels
by cache result: unattributed=12,209
by outcome reason: unrecorded=12,209
by attribution: unrecorded=12,209
by parser class: cacheable=3,825, declined=8,384
closed history: not considered (the active file alone was read)
```

The live host still runs the pre-change supervisor, so its rows carry neither
the version 2 fields nor an admission time. Every count above is `unrecorded`
because the recorder that wrote the rows never recorded it. That is the last
honest reading this plan can take before the service is upgraded; the
controlled real-sccache check is the evidence that new rows carry the fields.
The earlier "closed history: not considered" line is the active-file read the
`--store` flag performs; `--history` would read closed segments and report
their inventory.

The opt-in real-sccache checks pass at this commit:
`tests/test_sccache_outcome_observer.py` labels the controlled cold/warm pair,
the concurrent mixed pair, the declined probe, the timeout and the generation
change exactly as the table above records. That check now drives its recorder
through a wall clock, and asserts that the cold miss's row and its window
summary carry the same reading moment, so the controlled evidence covers the
label-timing fix as well as the attribution rules.

### Load mix and the limit of aggregate counters

The managed sccache server's cumulative counters at the same period were 11,834
compile requests: 6,732 hits, 1,393 misses, 3,617 requests the server declined
without compiling and 57 non-compilation probes. Hits were 82.9% of the 8,125
executed requests and misses 17.1%; `not_cacheable` requests were 30.6% of all
requests.

A 64-launch window of independent requests at that hit rate is unanimous with
probability below one in a hundred thousand; even an eight-launch window is
all-hit only about 22% of the time. Under continuous mixed load, sccache 0.14's
aggregate counters cannot identify an individual concurrent request, so the
honest outcome is a low label rate with a visible reason distribution. No
hit-rate target is claimed, and a mixed window is never split.

## Reconciliation and remaining loss

The layers measure different windows, so the report keeps their denominators
apart:

- **Observer**: 3,801 admitted requests and 3,835 published outcomes in the
  sampled quarter hour.
- **Retained store**: 15,846 outcome rows over 68 minutes in the copied file.
- **Snapshot**: 15,846 events, 13,412 identities, 0 labels.
- **Scored outcomes**: 0, so the benchmark reports `insufficient data` and
  `insufficient admission times`.

The 355-outcome difference between the observer's published events and the
writer's 3,480 appends in the quarter hour is not attributable from the
evidence retained here. Two causes are consistent with the counts: a launch
without a key or a label time is published as an event but cannot form a store
row, and an append that was queued inside the sampled window may complete after
it. The journal records neither case as a separate field, so this report treats
the difference as a hypothesis rather than a measured cause; a future
diagnostic would need a per-cause counter beside the writer's appended total.
Across the wider span, compaction reported 28,030 discarded rows; those rows
are outside the retained file and cannot be recovered by extraction. The
journal query is capped and says when its cap was reached; the store read is
bounded by its own retention policy.

### Which discarded rows retention removed and which a write failed

The observer's discarded total previously mixed two different losses. The
`store_reason` the writer already publishes is now counted into
`ObserverAccounting.by_store_reason`, so `expired` names a row retention
removed and `save_failed`, `seal_failed`, `corrupt` and `version_mismatch` name
a write the store could not keep. Only a writer result that actually discarded
a row contributes, so the map sums to `store_discarded` and a clean append adds
nothing to it. The dataset report prints the map and the snapshot loader
refuses a block whose map does not reconcile.

## Review follow-up: inventory, quarantine and report honesty

A second review round found four ways a report could state a count it had not
measured, and one dimension it had silently omitted:

- The closed-history scan was not told the recorded retention floor, so a
  prefix archive retention deleted on purpose was reported as gaps. The dataset
  command now reads the floor and passes it to the scan. A floor that exists
  but cannot be read, or a scan that fails, records the inventory as
  unavailable and keeps the rows already read; no gap count is derived from an
  unknown floor. A fresh store, whose state is `missing`, still scans with no
  floor.
- `Inventory` never populated its quarantine list, so the dataset report's
  `quarantined` count was always a false measured zero. The inventory now
  counts the regular files in the quarantine directory, because a quarantined
  file's name carries its fault reason and a random token rather than a
  sequence. A directory that cannot be listed makes the inventory unavailable.
- The plain dataset report printed a truncated journal's observer counts
  without saying they were partial. It now prints the same sentence the
  Markdown report uses, and a complete window prints nothing extra.
- The Markdown report's measured closed-history bullet omitted the quarantined
  count that the plain report prints. It now includes it; the active-only and
  inventory-unavailable bullets are unchanged.
- The snapshot loader joined the diagnostics block to the events by label,
  row count and admission presence, but not by origin, so a document whose
  event origin was changed from `registry` to `git` was accepted. The
  per-origin tally is now compared against `by_origin`, and a single changed
  origin is refused.
- The opt-in real-sccache check injected a fixed generation for every ordinary
  read, so the socket and `/proc` identity path was never exercised. The
  controlled cold/warm/mixed sequence now drives a plain `PeerServerProcess`;
  the injectable slot remains only for the deliberate generation-mismatch case.
- A default-run test now drives admission-time propagation through the shell:
  a cached entry carrying `admitted_at`, a clean ending, and one completed
  reading, asserting the row's admission time is the entry's and its label time
  is the reading's.

## Review follow-up: label-time provenance, omitted outcomes and append totals

A later review round found that the label-timing fix was not enough on its own,
because a version 2 store row cannot say which code dated it, and that several
reported totals could not be reconciled. ADR 011 records the decisions as D6,
D7 and D8.

### A pre-fix label can no longer leak into the replay

Old version 2 rows were stamped with the launch's completion time, so their
`observed_at` is only a lower bound on when the label became known. The dataset
adapter copies that time, and `admission_schedule` released the label at it, so
a prediction admitted between completion and the query could read a label it
could not have seen. The dataset command cannot distinguish a pre-fix row from
a fixed one — both are version 2 store rows — so it must not claim verification
for either.

The snapshot format is version 3, and each event carries an explicit
`label_observed_verified` boolean. A per-event marker was chosen over a
snapshot-wide `label_time_provenance` name because the fact is a property of
one row: a snapshot that later holds rows of mixed provenance can state both,
which a single document-wide marker cannot. The dataset command emits every
version 2 store row as unverified; the version 3 store row that carries its own
marker, added by the later review round below, is the first source that can
emit a verified event.

`events.scored_label` returns None for an unverified hit or miss, so training,
scoring and the phase schedule all withhold it through one rule. The replay
counts them as `unverified_label_time`, and both reports name the count. A
store history therefore now reports `insufficient data` with the withheld count
rather than scoring an unsound release time; that is a visible change for an
operator, recorded in the migration guide.

`tests/test_outcome_recorder_evidence.py` drives the recorder-to-replay path:
an admission between a launch's completion and the query return sees no label
from that reading, and a genuinely verified label at the same moment *is*
released to a later admission, so the exclusion is measured rather than vacuous.

### A published outcome that produced no row is counted

`outcome_shell_keys.cache_key` can return None when the salt or the crate
identity is unavailable, and `_record_for` then emits no store row. The
writer's discard breakdown never saw those outcomes, so the difference between
published events and stored rows stayed a hypothesis. The recorder now computes
one closed `RecordOmission` reason per launch and copies it onto the event, and
`ObserverAccounting.record_omitted` sums them by reason. The events and the
rows are built from the same launch in one transition, so an omitted outcome
and its missing row are one decision, not a difference inferred from two
totals. Both reports print the total and its breakdown, and the journal codec
requires the map to sum to it. The later review round below corrected the
sentence those reports state and made an absent reason count as unrecorded
rather than as a measured zero.

### The append total counts appends only

`observer_accounting` summed `store_records` across every `outcome_store`
operation, so a compaction's retained rows and a seal's released rows were
counted again while the reports called the total "store appends". Only
`operation == "append"` contributes now, and `by_store_operation` keeps every
operation visible. The dataset report and the rendered Markdown both say "rows
appended" and state the rule. `metrics_report`'s store section applies the same
rule to the host-facing report.

### Further coverage

`tests/test_predict_threshold.py` now holds the label count at the packaged
2,000 and exercises a partly usable history: 2,000 verified labels with five
usable admission times reports `insufficient held-out evidence`, so one usable
row is never treated as enough and an all-unusable history is not the only
shortage the report can name. A separate case proves a store-derived history is
withheld for its unverified times whatever its admission coverage.

`tests/test_predict_loss_accounting_properties.py` adds the property tests: the
timestamp round trip and the UTC offset rules of `outcome_record_time`, the
diagnostics decoder's accept-and-refuse contract over generated counts, and the
recorder's rule that every completion sharing one reading moment records that
moment.

## Review follow-up: the store states its own provenance

A second review round found that two of the previous round's fixes could not
take effect, and that several statements overstated what the other changes
bought. ADR 011 records the store decision as D9.

### The omission reason reaches the journal at all

`outcome_telemetry` published `record_omitted` as a field of every
`cache_outcome` event, but the metrics schema listed it among the counters.
`Metrics.emit` refuses a token under a counter's kind, so it dropped the field
from every event and the observer could never reconcile published outcomes with
appended rows. The field is now a schema token with the closed `RecordOmission`
vocabulary. One event describes one outcome, so an omission count is derived
from the events rather than published beside them, and no counter field is
needed. A test emits an event through the real schema and asserts the reason
survives, and a second asserts an unknown reason is refused while the loss is
counted.

### A current row can be labelled, dated and scored

The previous round made every store row unverified, which was correct for
history already written but left no path by which a correctly recorded row
could ever be scored. The store is now version 3 and an outcome row carries
`label_time_provenance`, written `verified` by the recorder that dates a row at
its statistics reading. Version 1 and 2 rows decode it as `None`, so their
treatment is unchanged and nothing is retroactively promoted. The accepted row
shape is fixed by the document's own version, so a version 3 row that omits the
marker is corrupt, and `encode_line` refuses to write a verified row into an
older document. `tests/test_predict_label_provenance.py` drives one delayed
reading through the real encoder and codec at both versions: the version 2
history withholds the label and the version 3 history releases it to a later
admission.

### Reports no longer skip or overstate their evidence

- The plain dataset report returned early when a snapshot carried no
  store-diagnostics block, which also suppressed the journal's observer counts.
  The journal block is now reported whichever way the store block goes, and a
  test covers each branch.
- An older `cache_outcome` event without an omission field contributed zero,
  so a window of them read exactly like a window whose outcomes were all stored.
  `ObserverAccounting.record_omitted_unrecorded` counts them, both reports
  name the count, and the journal codec requires the omitted and unrecorded
  totals together to fit inside the outcomes the window held.
- The reconciliation sentence claimed a published outcome reconciles as one
  appended row plus one omitted one. That is not the identity the counts prove:
  a failed append lands in `store_discarded` with a reason, a successful solo
  reading appends an outcome row *and* a window summary, and a queued write may
  be outstanding. Code, ADR and test now state the correct identity and that a
  summary is not an outcome row.
- The users' guide and this plan implied that recording new rows, or admission
  times, restores benchmark eligibility. A verified synthetic snapshot can pass
  both gates, but store-derived history also needs a source that proves its
  label-time provenance, and admission coverage alone is not enough.

### Further compatibility and schema coverage

`tests/test_predict_snapshot_diagnostics.py` parameterizes the diagnostics
round trip over versions 2 and 3, so the legacy shape is genuinely exercised
rather than a version 3 document with a downgraded header. The store-shape,
metrics-report and archive-plan fixtures were updated for the wider row:
`outcome_archive_helpers.segment_bytes_for` derives a segment bound from the
row the fixture writes, so a field added to the row can no longer repartition a
scenario about the plan's shape.

## Limitations

- sccache 0.14 exposes no supported per-request hit/miss signal. Its
  `SCCACHE_LOG=debug` output can print `Cache hit` and `Cache miss` lines, but
  that is a debug log, not a versioned API, and it names the compiler output;
  the recorder does not parse it. A future backend with a supported signal may
  be added behind the observation port in
  [ADR 007](../adrs/adr-0007-cache-outcome-observation-port.md).
- The live supervisor still runs the previous recorder. New rows with
  admission times, reason evidence and label-time provenance require an
  operator upgrade and restart; this change does not modify the service.
- Version 1 history remains unscorable by design. No admission time is derived
  from `observed_at - runtime_seconds`, and no missing hit is read as a miss.
- Version 2 rows written by the earlier build of this branch carry a
  launch-derived `observed_at` that may precede the label's true knowledge
  time. They remain readable, but the dataset command emits their label times
  as unverified and the replay withholds them, because the format version
  cannot identify which code dated a row. No historical row is repaired or
  re-stamped. Version 3 rows state their provenance, so a history recorded by
  the fixed recorder is scored, but no older row can be promoted into one.
- A published outcome that produced no store row is counted by cause, not
  recovered: its row is gone by design, because a launch with no key cannot be
  correlated with its admission. The count makes the loss visible rather than
  closing it.
- The archive byte budget sees larger rows. A policy sized exactly to the old
  row size will retain fewer rows; the retention tests and the documented
  budget are updated accordingly.
- Label loss by sccache generation is reported as unavailable. A store row's
  key carries the server version, not the generation the recorder observed, and
  no field for it was added: a field only future rows would carry could not be
  reconciled with the retained history, so the dimension is named rather than
  guessed. The label-time provenance field was added for the different reason
  that a row can state a fact about itself without any cross-row reconciliation.

## Verification

```sh
make check-fmt
make lint
make typecheck
make test PYTEST_XDIST_WORKERS=8
make markdownlint
make nixie
make validate
make audit
BUILD_LIMITS_SCCACHE_HOST_CHECK=1 uv run pytest -v -m sccache_host_check \
  tests/test_sccache_outcome_observer.py
make predict-dataset
make predict-benchmark
```

The last two report the sufficiency counts and keep the
`insufficient data`/`insufficient admission times` statuses until genuine rows
meet both gates.

`tests/test_predict_threshold.py` confirms the gates are separate at the
packaged `minimum_labelled_events=2000`. A snapshot of 2,000 verified labels
with no usable admission time reports `insufficient admission times`, and the
same labels with usable admission times are scored. A history with exactly five
usable admission times reports `insufficient held-out evidence`, so a handful
of paired rows is not treated as a verdict. A version 2 store-derived history
is withheld as `insufficient data` whatever its admission coverage, because
that row proves no label time; the run names the withheld count. Version 3 rows
do prove theirs, so the same gates admit a history the fixed recorder wrote.
Both cases keep the label gate counting labelled rows correctly, so a status
that differs is the other evidence's doing.
