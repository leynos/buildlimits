# Architectural decision record (ADR) 004: cache-outcome recording

## Status

Proposed. The owner approved this decision set as part of the cache-hit
prediction ExecPlan on 2026-10-23. Accept this ADR when EP-M8 merges, after the
W1 evidence and the H2 child-sighting implementation are reviewed.

## Date

2026-10-24.

## Context and problem statement

RFC 0002 records that buildlimits cannot currently distinguish an sccache hit
from a miss. The `CompileFinished` response carries no result, and sccache
0.14.0 exposes aggregate counters only. A statistics delta can identify a
result only when the requests inside it are isolated or unanimous. The approved
G0 measurement bounds ordinary solo-window coverage below H1's 5% floor, so W1
also needs the H2 child-sighting evidence.

The implementation must observe the server without changing compile admission,
avoid spawning a process on the supervisor's loop, and keep request-derived
values local. The recording rules, H1 to H5 thresholds and H2 classifier must
be fixed before new statistics or outcome records are read.

## Decision Drivers

- Keep compiler admission and token accounting unchanged.
- Attribute only balanced, explainable sccache counter deltas.
- Bound I/O, memory, query time and retry behaviour.
- Persist useful evidence locally without writing raw paths or compiler
  arguments.
- Fix thresholds and classifiers before collecting the data they judge.
- Keep every recorder failure observational and isolated from supervisor
  health.

## Options considered

### Option A: follow the RFC's unit-properties port literally

Reading `InvocationID` through `systemctl show` around each statistics query
would spawn a blocking process on the supervisor's event loop. It also inherits
the manager-transport fallback gap. Reading the service cgroup instead fails
when the unit is inactive. Rejected in favour of identifying the process that
answers the statistics query directly through its managed Unix socket.

### Option B: infer results from a lookup probe or a reimplemented key

sccache provides no lookup-only probe. Reimplementing its cache key would run a
dep-info pass outside admission, hash inputs and couple buildlimits to a
versioned upstream implementation. Rejected. An upstream result field would
improve attribution for all sccache users, but is deferred and is not required
for this host-compatible design.

### Option C: observe bounded statistics windows and local child sightings

Use the managed socket and `/proc` to identify the statistics server; classify
only balanced counter windows; sample child compiler arguments in memory; and
write bounded, HMAC-keyed records through a dedicated writer. Chosen for W1.

## Decision outcome / proposed direction

W1 is EP-M0 to EP-M8. EP-M9 is W2 and remains gated on the owner's G1 decision.
No later RFC 0002 workstream is included.

This ADR refines the observation-only scope of
[ADR 003](adr-003-bounded-restart-tracing.md): cache outcomes are local
evidence, not admission hints. It adds no per-compile trace span and does not
change the existing trace schema.

### D2: identify the statistics server without spawning

Read the server generation synchronously before and after a statistics query:

1. Connect non-blockingly to the managed `AF_UNIX` socket.
2. Read the listener PID from `SO_PEERCRED`.
3. Read `/proc/<pid>/stat` field 22 for the process start time.
4. Read `INVOCATION_ID` from `/proc/<pid>/environ`.

The generation is `(INVOCATION_ID or "", pid, starttime)`. A refused connect
means no server; any other failure is `generation_unavailable`. This identifies
the process serving the query and detects a restart inside one service
invocation. It avoids the blocking `systemctl show` alternative and the
inactive-unit gap of reading the unit cgroup.

### D3: discard readings that straddle a launch

Discard a statistics query as `straddled_reading` if any cached group launches
during it. The result is neither a window end nor a baseline, so every launch
belongs to exactly one window.

### D4-D5: extend result and reason vocabularies

Add `declined` (`requests_not_cacheable`), `not_compile`
(`requests_not_compile`) and `unsupported` (`requests_unsupported_compiler`) to
`cache_result` for server declines that do not compile. Add
`unsupported_version` to `outcome_reason`. Only sccache version `0.14.0` is
supported; another version disables recording until restart. This distinguishes
requests such as Cargo's `-vV` probe from a compile that ran and returned
`not_cacheable`.

### D6: separate group, reading and store events

Register three supervisor events:

- `cache_outcome`, once per cached launch while recording is on, with
  `cache_result`, `outcome_attribution`, `crate_origin`, `outcome_reason`,
  `runtime_seconds` and `window_groups`.
- `outcome_reading`, emitted when the reading reason changes and once a minute
  as a summary of `readings`, `readings_skipped` and `readings_failed`.
- `outcome_store`, with `store_operation` (`load`, `append`, `compact` or
  `salt`), `store_reason`, `store_records` and `store_discarded`.

Use the qualified field names because the journal's `FIELD_KINDS` namespace is
flat. Separate event grains prevent double-counting and journal flooding.

### D7: protect every digest with a local salt

Use HMAC-SHA-256 under the local salt for every digest. Argument digests cover
absolute paths, so unsalted hashes would disclose linkable path information.

### D8: bound the reading sequencer

Start a reading only when every quiet-point gate in D20 holds and either no
baseline exists or the open window contains an ended launch. Start at most one
query every `OUTCOME_READ_SECONDS = 1.0`; permit only one in flight; and cap
each statistics query at `OUTCOME_QUERY_SECONDS = 2.0`. Repeated failures use
exponential back-off up to 60 seconds.

### D9: keep the real-binary check private and non-blocking

The opt-in, 1.5-hour host check uses real sccache 0.14.0 on a private socket
and private `SCCACHE_DIR`, driven by a scripted fake `rustc`. It does not
bypass admission and does not block the other milestones. The live
managed-socket fixture is captured read-only in EP-M0. If the private check is
a no-go, use a documented, admitted cold-then-warm operator procedure after
release; this fallback observes trailing counters indirectly, so D12 becomes
mandatory.

### D10: define toolchain identity without running the compiler

The pure identity tuple contains the HMAC of the compiler argument, the HMAC of
`RUSTUP_TOOLCHAIN` (or the empty string), sccache's Rust `CACHE_VERSION`
(`"6"`) and the server version in the statistics document. The supervisor does
not run `<compiler> -vV`, resolve the compiler or stat its files. Cargo already
includes `-vV` output in `-C metadata`, which is covered by the argument
digest. H5 measures the residual risk of a rustup channel or sysroot changing
in place.

### D11: fix H1-H5 thresholds before collection

The following thresholds are fixed from RFC 0002's H1-H5 definitions and the
approved ExecPlan's Table B. Every overall verdict requires at least 2,000
attributed outcomes; H2 also requires at least 100 evaluable records. H1's
controlled-label check requires at least 200 windows. Results between a
prediction threshold and its falsification threshold are inconclusive.

| Hypothesis | Prediction                                                                                                                     | Falsified if                           |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------- |
| H1         | No attributed label disagrees with truth in at least 200 controlled windows; host coverage is at least 20% of cached requests. | Any wrong label, or coverage below 5%. |
| H2         | Wall time plus a sighted child agrees with at least 95% of window labels.                                                      | Agreement below 90%.                   |
| H3         | Every request with a C4 feature is declined as uncacheable, and none without one is.                                           | One counterexample.                    |
| H4         | Registry crates at a pinned version with an unchanged argument digest hit at least 95% after their first observation.          | Below 90%.                             |
| H5         | At most 5% of consecutive outcome pairs within one key flip without a toolchain change.                                        | Above 10%.                             |

_Table 1: Fixed H1-H5 hypothesis thresholds._

### D12: confirm provisional labels only if needed

If EP-M1 finds a trailing counter, a provisional label is confirmed only when
the next accepted window in the same generation balances exactly. Keep at most
one pending attribution; otherwise emit `unattributed` with `count_mismatch`.
This contingency is inactive unless the real-binary check demonstrates the
counter behaviour.

### D13-D16: classify readings and isolate repeated failures

Use this first-applicable reading-reason order:

1. `read_error` (query failure or launch failure).
2. `parse_error` (not one JSON object with a `stats` object).
3. `unsupported_version`.
4. `generation_unavailable`.
5. `generation_changed`.
6. `empty_stats` while the window has a launch.
7. `counter_decreased`.
8. `ineligible_window` for an unclean group.
9. `count_mismatch` when launch counts, classification counters or executed
   request footprints do not balance.
10. `mixed_window` for a balanced window the counters cannot split.

Accept an all-zero baseline in a running generation. Add `straddled_reading`,
`supervisor_stopped`, `no_baseline`, `generation_unavailable`,
`window_overflow` and `recorder_disabled` to `outcome_reason`. Keep
`no_quiet_point` for the aggregate `readings_skipped` counter. Back off
identical failures at 1, 2, 4 and onward to 60 seconds, resetting the interval
after success. Stop readings until restart for `unsupported_version` or
`recorder_disabled`; expose that state in the status document and an
`outcome_reading` event.

### D17: own a bounded, append-only local store

Store JSON Lines at `$XDG_STATE_HOME/buildlimits/cache-outcomes.jsonl`,
defaulting to `~/.local/state/buildlimits/`. Set the directory to mode 0700 and
files to 0600. The first line is
`{"format": "buildlimits-cache-outcomes",
"version": 1, "salt_id": …, "written_at": …}`.
A single writer thread owns every file operation; the loop appends through a
bounded queue of 4,096 entries and drops overflow with `save_failed`.

Compact at start and stop. Drop records older than 30 days by `observed_at`,
keep the 10,000 newest by insertion sequence, then `fsync` and `os.replace` a
fixed temporary name. Drop a torn final line on load. Treat files over 16 MiB
as corrupt; move corrupt or version-mismatched files aside once to
`cache-outcomes.jsonl.bad`; never overwrite a newer format. At stop, enqueue
compaction after `settle_token_request` and `requests.close_all`, then join the
writer for at most one second. This preserves atomic compaction without a
once-a-minute rewrite that could write up to 5.8 GB a day or stall the loop.

### D18-D20: keep release compatibility and the quiet-point gate

EP-M6 is an inert, releasable plateau. It accepts `cache_outcomes = "off"` or
`"record"`; `record` logs once that recording is not built in yet. This lets a
downgrade read a dev-env-rocky rendered `supervisor.toml`; document removing
the key before downgrading past the plateau.

Wrap every loop-called `OutcomeShell` method. A caught exception logs once,
emits `recorder_disabled`, switches to a null shell until restart and returns.
Place hooks after the admission call they observe: the launch hook follows
`Groups.release`; the emptied hook snapshots `(group_id, cached, clean)` before
`group_emptied` and calls the shell after `_apply`.

The pure `outcome_gate.quiet_point(inputs)` requires `queued == 0`,
`cached_moving == 0`, `server_drain == 0`, cached admission, `cache_allowed()`,
idle recovery, no restart barrier file and no in-flight query. Read recovery
idle last through `CacheRecovery.describe(now)["phase"]`. The `queued == 0`
clause prevents the next queued launch from straddling the query.

### D21-D23: define H3 evidence and deliver its replay in W1

`cacheability_features(argv)` includes incremental compilation, crate type,
multiple inputs, `--emit` without `link`, `--crate-name` or `--out-dir`,
missing input (including Cargo's `-vV`) and sccache's `NotCompilation` flags.
For every balanced window, including mixed windows, store a summary with the
launch count, delta of `requests_not_cacheable` and count classified as
uncacheable. H3 uses those totals and per-request rows from solo windows; equal
counts are necessary but not sufficient.

Ship `build-limits-cache-replay h3` read-only over the store in W1 (EP-M7), so
G1 has the H3 verdict. The owner's Q1 answer supersedes the earlier proposal to
omit child sightings: EP-M8 collects H2 evidence in W1. `not_collected` applies
only to stores written before child sampling existed.

### D24-D25: retain five ports and fix H2 child-sighting rules

Extend the RFC's unit-properties port to `ServerProcessPort`, with synchronous
`generation()` (D2) and `full_emit_metadata()` operations on the same server
process. Find its control group from `/proc/<pid>/cgroup`, cache the directory
per generation, and enumerate members through `cgroups.member_pids`. Read each
new `(pid, starttime)` command line once. Inspect at most 64 processes per
sample and forget a classification when its process leaves the group.

Extract the request's own `-C metadata` value from `-C metadata=X`,
`-Cmetadata=X`, `--codegen metadata=X` or `--codegen=metadata=X`, and keep it
in memory only. Count a `rustc` or `clippy-driver` child as a full-emit compile
if `--emit` contains `link` or `metadata`, or is absent; dep-info-only passes
do not count. Every 0.25 seconds, while recording and `cached_moving > 0`,
sample each running cached entry. Mark it `seen` if its metadata appears;
failures back off from 0.25 to 8 seconds and are counted.

Each event and record carries `child_sighting` (`seen`, `not_seen` after at
least one sample, or `unavailable` if never sampled or no request metadata) and
`child_samples`. For records labelled `hit` or `miss`, walk each key in
insertion order and predict `miss` when a child is seen; otherwise predict
`miss` when an earlier hit exists and runtime exceeds the greater of 1 second
or three times the median of earlier hits; otherwise predict `hit`. Evaluate
only records with a sample whose key has both outcomes. Report `supported` at
95% agreement or more, `refuted` below 90%, and `inconclusive` between. Report
`insufficient` below 2,000 attributed outcomes or 100 evaluable records. Also
report the share of labelled misses with a sighting and each clue's agreement.

## Goals and non-goals

### Goals

- Record explainable cache outcomes without influencing admission.
- Keep evidence local, bounded and useful for H1-H5 evaluation.
- Make G1 a decision based on recorded evidence and fixed thresholds.

### Non-goals

- Infer or display predicted outcomes in status or metrics.
- Change token accounting, compiler admission or recovery behaviour.
- Implement RFC 0002 W3-W6 or decide the result of G1.
- Persist raw compiler arguments, paths or child command lines.

## Migration plan

1. Record this proposed design, Table 1 and the W1/W2 boundary in EP-M0 before
   collecting the live statistics fixture.
2. Implement W1 through EP-M8. The host compatibility check is opt-in; all
   other evidence uses the admitted supervisor and the local store.
3. At G1, decide whether to release EP-M9. Do not start it before that
   decision.

## Known risks and limitations

- Quiet and unanimous windows favour low-activity periods; every verdict must
  report its limited representativeness.
- Counter windows can be unattributable after a restart, mixed result or
  unsupported server version; such windows must remain unlabelled.
- HMAC protects stored digests, but local records and their timing still reveal
  build activity to the host account that can read them.
- Child sampling can miss short-lived processes. `unavailable` and sample
  counts make that limitation visible rather than treating absence as a miss.
- An in-place sysroot change is not in the toolchain identity and remains a
  confounder measured by H5.

## Outstanding decisions

- At G1, decide whether W2 (EP-M9) should proceed, narrow or stop.
- Any work after W2, including an advisory prediction, ordering, shadow mode
  or direct composition, requires the RFC's later gates and separate approval.
