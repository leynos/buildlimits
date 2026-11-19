# Archive cache outcomes as XZ through a timer-friendly CLI

Status: IN PROGRESS

## Purpose and observable outcome

The outcome recorder stores one JSON object per line in
`$XDG_STATE_HOME/buildlimits/cache-outcomes.jsonl`. Rohga records about 6.8
rows/second at roughly 661 bytes/row, which extrapolates to about 11.7 GB of
uncompressed JSON Lines over 30 days. The current store keeps at most 12 MiB
and 10,000 rows, so the requested 30-day span is impossible today.

After this change an operator can install one timer-friendly command,
`build-limits-outcome-archive`, and run it from a systemd user timer. The
command seals the supervisor's active file only through the writer, compresses
closed segments to XZ in bounded memory, verifies each archive before
publishing it, applies the configured 30-day / 15,000,000,000-byte retention
policy, and reports what it did. Replay and dataset export can read the whole
retained history in chronological order across archived, sealed and active
segments.

A human can verify the outcome by running:

```bash
build-limits-outcome-archive inspect
build-limits-outcome-archive run --dry-run
build-limits-outcome-archive run
build-limits-cache-replay h3 --history
```

`inspect` and `--dry-run` change nothing. `run` is safe to repeat, refuses to
overlap another run with exit status 75, keeps the uncompressed source until a
verified archive is published, and leaves compile admission untouched.

## Conformance basis

- Repository standards: `AGENTS.md` (code style, quality gates, documentation
  maintenance) and `docs/documentation-style-guide.md`.
- Existing decisions: [ADR 005](../adrs/adr-0005-cache-outcome-recording.md)
  (the local outcome store and its privacy boundary) and
  [ADR 007](../adrs/adr-0007-cache-outcome-observation-port.md) (the evidence
  boundary). A new ADR, 0010, records the segment, archive and retention
  decisions; ADR 008 is already used by the model-check tooling.
- Upstream requirement: <https://github.com/leynos/buildlimits/issues/63>.
  The retention-policy contract is
  <https://github.com/leynos/buildlimits/issues/64>; this change implements the
  same policy so configuration can ship with or without compression. Permanent
  host provisioning belongs to
  <https://github.com/leynos/dev-env-rocky/issues/424>.
- Downstream consumer: the longitudinal predictor and ensemble benchmark in
  issue #60 (`buildlimits/predict/`).

## Constraints

- The supervisor writer is the only process that may append to, truncate,
  replace or rename the active `cache-outcomes.jsonl`. The archiver never opens
  the active file for writing, never takes the writer's queue and never takes
  the archiver lock on the writer thread.
- A sealed segment is immutable until the archiver publishes its verified XZ
  archive. The uncompressed source is unlinked only after the archive, its
  sidecar and the archive directory are durable.
- All archive work is streamed in fixed-size chunks. Peak memory depends on
  the XZ preset and chunk size, not on retained history size.
- The byte budget counts logical uncompressed retained bytes: active and sealed
  files by `stat`, archived segments by their sidecar's `logical_bytes`.
  Temporary disk headroom is a separate bound.
- Retention evicts oldest closed history first. A record whose observation time
  equals the cutoff is retained. Below both bounds, history is retained with no
  hidden count cap.
- Archives keep the version-1 record schema and the salt identifier. Salt
  changes partition history; no cross-salt comparison is implied.
- Archival failure never changes admission order, compiler tokens or queue
  policy. It is observable through exit statuses, `last-run.json`, `inspect`
  and the store's `outcome_store` telemetry.

## Tolerances (exception triggers)

- Stop and report if a change would make the supervisor append to or replace a
  sealed or archived file, or make the archiver write the active file.
- Stop and report if bounded-memory verification cannot be demonstrated at the
  fixed `tracemalloc` bounds.
- Stop and report if a required gate fails for a reason that is not a defect in
  this change.
- ExecPlan files are excluded from source-size budgets, as the repository
  standards require.

## Risks

- A crash between archive publication and source removal leaves a duplicate
  sealed source. Mitigation: every run reconciles duplicates by verifying the
  archive against the source, then removing the source.
- A crash after the sequence counter advances but before the active rename
  leaves a sequence gap. Mitigation: inventory reports gaps and the writer
  initializes the next sequence from the maximum of the counter and every
  listed sequence.
- Retention trimming an archived segment requires recompression. Mitigation:
  stream, verify, publish through a private temporary file, and keep the old
  archive until the replacement is durable.
- Sparse labels remain the larger predictor problem; compression does not
  create outcome labels. This is stated in the operator documentation.

## Progress

- [x] Orientation: read the store, codec, config, replay and predictor code.
- [x] Retention policy and configuration contract.
- [x] Streaming codecs, segment layout and inventory.
- [x] Writer-owned sealing: `StoreWriter` seals at the segment high-water mark,
  publishes the sequence counter and sidecar metadata before the rename, emits
  `seal`/`seal_failed`/`archive_backlog`, and keeps the legacy module bounds
  live when no policy is configured. Focused store tests pass.
- [x] Archiver, verification, publication and retention execution. A run
  reconciles, archives, heals, trims and deletes under the private lock, then
  replans over the settled store so one run leaves the byte budget true.
- [x] Timer-friendly CLI. `build-limits-outcome-archive run|inspect` reports
  bounded status lines or JSON and exits 0/1/2/3/75.
- [x] Archive-aware history, replay and dataset integration. `outcome_history`
  reads archived, sealed and active documents in append order; the replay and
  dataset commands gained `--history`, `--salt-id` and `--all-salts`.
- [x] Memory tests: the benchmark reports the per-chunk peak, and a traced
  allocation test holds a multi-megabyte segment under one mebibyte.
- [x] Benchmark: `make outcome-archive-bench` prints a JSON report.
- [x] Fault, boundary and replay-equivalence tests. New modules cover the
  retention policy, segments and inventory, the line stream and XZ codec, the
  pure planner, the filesystem adapters, archive execution, the CLI, and
  writer-owned sealing, including the generated-document property test.
- [x] Documentation (2026-11-19): ADR 0010, the users' guide section and
  retention keys, the developers' guide §9.1 and module inventory, the
  repository layout table, the documentation contents index, the README and the
  migration guide's console-script list.
- [x] Gate remediation (2026-11-19): every module and shared test helper is at
  or below the 400-line limit after splitting the store state and sealing, the
  archive context, rows, encode, lock, quarantine and run operations, the
  segment sidecar, state and inventory, the config retention and value rules,
  the record encoder, and the oversized archive, planner and segment tests.
  Repeated blocks were extracted where one owner exists; the rest are recorded
  as reasoned `[[tool.duplication_gate.allow]]` exceptions that name every
  location in the family.
- [x] Full gate run (2026-11-19): `make fmt`, `make check-fmt`, `make lint`
  (including the duplication gate at 246 reasoned exceptions), `make typecheck`,
  `make audit`, `make markdownlint` and `make nixie` pass, and `make test`
  reports 4358 passed, 85 skipped and one failure. The failure is
  `tests/test_harness_ownership.py` on the free-threaded Python 3.15
  interpreter, which this branch does not touch (see the decision log).
- [x] Publication (2026-11-19): the branch is pushed over SSH and tracked, and
  draft pull request <https://github.com/leynos/buildlimits/pull/65> targets
  `main`. GitHub reports no checks configured for the branch, so the local
  gates above are the acceptance evidence. The first CodeRabbit pass returned
  its review rate-limit response; retries continue under `scrutineer`.
- [x] Review remediation (2026-11-19): every CodeRabbit finding is implemented
  and checked against the source before changing it. Inventory listing failures
  now propagate instead of reading as an empty store; a sealed source removed
  after the scan is read from the archive that replaced it; the archive CLI
  prints `strerror` rather than a path; the benchmark measures a supplied
  segment and generates a header row; the replay CLI reports store faults
  separately; trim accounting counts dropped bytes so the header is inside the
  allowance; the retention policy refuses a segment larger than the compaction
  bound; a seal whose directory sync fails keeps its rows closed; and sidecars,
  retention state and the sequence counter are read through the private,
  bounded reader.
- [x] Test hardening (2026-11-19): sidecar symlinks and oversized sidecars and
  retention records are corrupt or absent rather than readable; the sequence
  counter reports a link or permission fault; the published sidecar has exactly
  the fifteen documented fields; the benchmark, trim and history tests assert
  the new boundaries. `tests/test_harness_ownership.py` now uses
  `resource.RLIM_INFINITY` instead of `-1`, so the Python 3.15 free-threaded
  failure recorded above is gone.

## Surprises & discoveries

- `outcome_record_codec` already exists and houses the strict JSON Lines
  encoding; per-line decoding will be added there rather than duplicated.
- ADR 008 is already taken by the model-check decision, so the archive decision
  becomes ADR 0010.
- The users' guide and migration guide still say six console scripts while the
  package already installs nine; with this change the count is ten.
- The writer's default policy is built on each access from the documented
  `outcome_records` bounds. Snapshotting it at construction froze the limits
  and made the existing high-water-mark test unable to shrink the bound.
- A record offered to a writer whose startup has not settled is discarded as
  `save_failed`. Tests that build a segmented store must wait for the startup
  `load` result before appending, exactly as the writer's own tests do.
- The first `seal` of an empty store must not claim a sequence, or the store
  would begin with a permanent gap. The writer installs a header-only active
  file instead.
- A fresh segmented store had no active file at startup, because appends open
  the file without creating it. The first record of every new store was
  therefore discarded as `save_failed`, and only the second row, whose retry
  seals an empty segment, created the file. Startup now installs the header
  whenever the active file is missing or empty.
- The source's torn-tail flag cannot be recovered by scanning the finished
  archive, which is always complete. Publication now carries the writing pass's
  flag into the sidecar so the repair stays recorded.
- A whole-archive deletion that exactly met the byte budget was followed by a
  trim of the next archive to its own size. The planner now stops byte work as
  soon as the excess reaches zero.
- The retained-suffix test in a byte trim compared only the rows after the
  current one, so a trim could keep one row more than its allowance. The
  current row's bytes are now included before comparing with the allowance.
- The duplication checker resolves a family member to `path::name` only when
  the matched span starts at a named definition. A single matched line inside a
  function, or a fragment that begins mid-function, has no name, so an allow
  entry has to name that member by path alone. A family entirely inside one
  file is also rejected by `members` (it requires two or more entries) and
  needs the separate `[[tool.duplication_gate.unit]]` table.

## Decision log

- Writer owns the active file and seals by atomic rename; archiver owns sealed
  and archived files under its own `flock`. This removes every copy-truncate
  and writer/archiver race without a shared lock.
- The retention budget is logical uncompressed bytes. Physical XZ bytes and
  temporary headroom are reported separately so the requested 15 GB history
  does not become a 15 GB per-file allowance.
- A record cap is disabled by default in segmented mode and remains 10,000 in
  legacy mode, preserving deployed behaviour while removing the silent cap from
  large policies.
- XZ uses Python's standard-library `lzma` with an explicit preset, CRC64
  integrity check and streamed chunks; no new runtime dependency is added.
- Segments are verified by decompressing the published temporary archive to
  EOF and comparing record count, logical bytes and SHA-256 with the source
  stream before any filesystem publication.
- History defaults to the newest salt domain and supports explicit selection
  or all salts. H2 remains a two-pass evaluation over a re-iterable stream that
  retains only per-key state; H3 consumes one pass.
- `--salt-id` and `--all-salts` imply history mode, while no flag keeps each
  command reading only the active store, so existing invocations are unchanged.
  Both commands share the option definitions in `outcome_history_args`.
- A segmented writer installs its header at startup so the first append always
  has an active file to reach; an empty seal still avoids claiming a sequence.
- Archive metadata records the torn tail observed while reading the source,
  not whichever flag a scan of the completed archive could infer.
- Byte planning stops once the excess reaches zero, so an exact deletion never
  churns the next archive.
- A trimmed row survives only when its whole retained suffix, including the
  row itself, fits the byte allowance.
- `tests/test_harness_ownership.py` failed on Python 3.15.2 free-threading
  because its parameter list spelled the infinite limit as the literal `-1`,
  while this build reports `resource.RLIM_INFINITY` as `18446744073709551615`.
  The test now uses the runtime constant, so the case passes on any supported
  interpreter. This is a test-portability fix, carried in its own commit.

## Outcomes & retrospective

The timer-facing archive command, its verification and retention execution, the
writer-owned sealing and the archive-aware replay and dataset paths are
implemented and covered by ten test modules. The command keeps paths out of its
successful output, maps every outcome to the documented exit status, and was
checked against the real system clock only in the redacted CLI snapshot.

The notable lesson is that the writer's startup path, not the archiver, was
where data could be lost: a fresh segmented store had no active file until its
second append. The regression test `test_a_fresh_store_keeps_its_first_row`
pins the fix. The other three defects found while writing fault tests (the
torn-tail flag, the exact-budget deletion, and the trim retained-suffix
comparison) each had a narrow blast radius but would have silently loosened the
retention contract.

## Verification plan

Invariants:

1. No byte is appended to the active file except by the writer thread.
2. A sealed or archived file is never truncated in place; replacement is by a
   private temporary file and an atomic rename.
3. An uncompressed source exists until a decompressed archive matches its
   record count, logical bytes and SHA-256.
4. Retained logical bytes never exceed the configured budget after a successful
   run unless one active segment plus one record transiently exceeds it.
5. Eviction is oldest-first; records at the cutoff survive.
6. Iterating history yields records in sequence order: archived or sealed
   segments ascending, then the active file.
7. Peak memory during archiving is bounded by chunk size and XZ preset, not by
   segment or history size.

Methods and evidence:

- Unit and example tests for the policy, codec, stream, inventory, planner,
  archiver, history and CLI outcomes, including every failure mode named by the
  issue.
- A property test that a generated history produces the same decoded record
  stream before and after archiving.
- Fixed `tracemalloc` bounds at two history sizes, plus an opt-in subprocess
  test with a several-hundred-megabyte history and a `ru_maxrss` bound.
- Boundary tests at exactly 30 days and exactly the byte cap, including sparse
  history below both bounds and an explicit record cap.
- A benchmark target reporting compression ratio, elapsed time, child peak RSS
  and peak temporary disk use on generated representative data.

Non-vacuity: every boundary test exhibits at least one record on each side of
the threshold, and the memory tests compare two sizes so a constant-buffer
failure or an accumulating buffer both fail.

## Revision note

- Initial draft created while implementing issue #63.
- Review remediation added the CodeRabbit fixes, the new boundary tests and the
  `RLIM_INFINITY` portability fix for `tests/test_harness_ownership.py`; the
  decision log's "report rather than change" entry was replaced accordingly.
