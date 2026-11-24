# Restore trustworthy cache hit/miss label coverage and admission-time evidence

This ExecPlan records the implementation and host evidence for
[issue #68](https://github.com/leynos/buildlimits/issues/68). It refines the
outcome store and recorder in
[ADR 005](../adrs/adr-0005-cache-outcome-recording.md) and is governed by
[ADR 011](../adrs/adr-0011-cache-outcome-admission-time-and-label-loss-accounting.md).
The benchmark algorithms in
[issue #60](https://github.com/leynos/buildlimits/issues/60) are unchanged.

## Status

Implemented on the issue branch, including the review follow-up recorded in
[Label timing and the store-format upgrade](#label-timing-and-the-store-format-upgrade)
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

- The store format is version 2. An outcome row carries `admitted_at` (UTC,
  captured where admission is decided), `outcome_reason` and
  `outcome_attribution`. Version 1 rows stay readable and decode those fields as
  `None`; they are never scored.
- The snapshot carries bounded diagnostics: retained rows by result, reason,
  attribution, origin, parser class, child sighting and sccache version;
  admission-time coverage; the eligible cacheable denominator; and closed
  history loss.
- The dataset command records the observer's own journal counts, including
  admitted requests by cache path and backend, published outcomes by result and
  reason, writer appends and evictions, and reading skips and failures.
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
launch-derived `observed_at`. They stay readable and scorable, but their
recorded time may precede the true knowledge time, and the format version
cannot identify them. No new timestamp field was added for old rows. The
benchmark releases a label no earlier than the moment its row records, which
for those rows is a lower bound rather than proof.

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
of every denominator, version 1 compatibility, the admission-time round trip,
and the gate changes. The benchmark tests show that the same store shape
without admission times predicts nothing, while a store whose rows carry them
meets the gate and is scored.

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

## Limitations

- sccache 0.14 exposes no supported per-request hit/miss signal. Its
  `SCCACHE_LOG=debug` output can print `Cache hit` and `Cache miss` lines, but
  that is a debug log, not a versioned API, and it names the compiler output;
  the recorder does not parse it. A future backend with a supported signal may
  be added behind the observation port in
  [ADR 007](../adrs/adr-0007-cache-outcome-observation-port.md).
- The live supervisor still runs the previous recorder. New rows with
  admission times and reason evidence require an operator upgrade and restart;
  this change does not modify the service.
- Version 1 history remains unscorable by design. No admission time is derived
  from `observed_at - runtime_seconds`, and no missing hit is read as a miss.
- Version 2 rows written by the earlier build of this branch carry a
  launch-derived `observed_at` that may precede the label's true knowledge
  time. They remain readable and scorable, and the store format version cannot
  identify them, so the benchmark releases each label no earlier than the
  recorded time. That is a lower bound, not proof, for those rows. No timestamp
  field was invented to mark them; a strictly ordered history is best started
  after the upgraded supervisor has run.
- The archive byte budget sees larger rows. A policy sized exactly to the old
  row size will retain fewer rows; the retention tests and the documented
  budget are updated accordingly.

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
