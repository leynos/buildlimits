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
- [x] Second review remediation (2026-11-20): the next CodeRabbit pass found
  thirteen defects, each confirmed in the source before it was changed.
  `ensure_private_directory` now creates missing components one at a time after
  proving each ancestor is a real directory and applies the mode through an
  `O_NOFOLLOW` descriptor, so a symlink cannot redirect creation or a `chmod`;
  the quarantine directory is validated the same way. Reconciliation writes the
  sidecar it verified before removing the only source that could regenerate it.
  A byte plan no longer trims an archive it also deletes. A heal-only run
  re-plans retention, so a repaired sidecar's real logical bytes are enforced
  in the same firing. A failed quarantine move of a faulted artefact is
  recorded and the run continues. History reading re-scans and repeats a
  bounded number of times, so rows sealed mid-read are not silently missed. The
  XZ decoder states a 128 MiB dictionary budget. Gap enumeration stops at 4096
  sequences and reports `gaps_truncated`, in the report, the JSON document and
  one plain status line. A lost sequence counter also reads the archive
  directory, so an archived name is never reused; an unmeasurable active file
  is reported rather than treated as empty. The writer drops a torn active tail
  before appending and rewrites a header-only file on a salt change. The
  dataset command refuses an `--output` that names any file it reads, including
  a closed segment reached through `--history`, and the benchmark streams its
  archive to a temporary file instead of its own memory. Every fix has a
  regression test; the eleven focused modules pass (142 tests).
- [x] Duplication-gate remediation (2026-11-20): the near channel merged several
  previously separate import-preamble families into one 30-member family as
  soon as a module gained the imports it reads, so the gate failed on families
  no existing allow entry named. Genuine duplication was extracted instead:
  `_now` was replaced by `utc_text.utc_now`, the archive filesystem path walk
  became `_components` plus `_require_directory_component`, and the byte
  planner's two candidate walks became one `_planned_entries` generator. The
  remaining test-shape, import-list and type-checking-preamble families are
  recorded as reasoned exceptions naming every member; the gate passes with 251
  reasoned exceptions. The new modules and the shared clock are documented in
  the developers' guide module inventory.
- [x] Assessment remediation (2026-11-20): the review's eight correctness gaps
  and its scale items are implemented, each with a regression test. Active-file
  age is enforced at restart; a torn tail that cannot be removed stops every
  append; a failed temporary archive leaves its readable source in place; a
  trim whose sidecar write fails is repaired by the next run, which reports the
  trimmed facts; the plan is built under the lock; history reads retry a
  bounded number of times and fail closed as `unsettled` or `vanished`; and the
  record cap now deletes whole oldest archives and trims the one that straddles
  it. Salt-domain retirement, the multi-domain provenance and the sidecar size
  check each have their own test. A generated 6,000-row, multi-segment history
  archives and replays under a traced peak below 128 MiB. The benchmark reports
  elapsed time, peak RSS and temporary disk, and `SEGMENT=` selects the file it
  measures. The users' guide documents the measured figures, `MemoryMax=512M`,
  `TimeoutStartSec=2h`, the explicit 15,000,000,000-byte policy and the four
  timer examples, which `tests/test_users_guide_examples.py` pins to the
  archiver's own parser.
- [x] Module-size remediation (2026-11-20): the commit gate's `C0302`
  `too-many-lines` finding is cleared by splitting six modules along the
  boundaries their responsibilities already had. `outcome_archive_dirs` takes
  the descriptor-relative directory walk out of `outcome_archive_fs`;
  `outcome_store_restart` takes the restart transitions out of
  `outcome_store_state`; `predict/render_tables` takes the four table builders
  out of `predict/render`; and the three over-size test modules each gained a
  support module (`render_helpers`, `history_fixtures`) or a sibling suite
  (`test_predict_render_provenance`, `test_predict_dataset_cli_journal`,
  `test_outcome_history_commands`). Two genuine duplications the detector found
  in the moved code were removed rather than allowed: the two identical
  surviving-plan generators in `outcome_archive_budget` are now one generic
  `_surviving`, and the directory walk's repeated `ELOOP`/`ENOTDIR` translation
  is one `_refuse_non_directory`. Every remaining new family is a recorded
  exception with its reason in `pyproject.toml`.
- [x] Coverage lift (2026-11-20): five new modules close the uncovered fault
  boundaries the review named. `test_outcome_history_settling` drives the
  reader's retry, the settle check that can never re-scan, and a header-less
  segment; `test_outcome_archive_fs_faults` forces each primitive's cleanup (a
  refused temporary, a publication whose directory sync fails, a failed state
  write or sync, a stale temporary that cannot be unlinked, an unreadable
  directory entry, and each ownership and component refusal);
  `test_outcome_archive_run_faults` drives a reconciliation, a failed
  reconcile, an unreadable source, a failed delete, and a record-cap trim
  through a whole run; `test_outcome_retention_bounds` covers the settings to
  policy mapping and every refused bound; and `test_predict_snapshot_domains`
  covers a snapshot spanning two salt domains. The injected-failure stand-in
  moved into `tests/outcome_archive_helpers.py::refuse` so the fault tests fail
  one way, and the two remaining families the detector reported are recorded
  with their reasons. Measured over the whole suite, statement coverage rises
  from 91.43% to 91.92%, above the 91.31% ratchet floor.
- [x] Whole-plan remediation (2026-11-20): the follow-up assessment's history
  gaps are closed. The record cap counts rows still in the active file, and the
  writer ages the active file on append as well as at restart, sharing the
  clock-skew flag with compaction. History reads pin one snapshot
  (`outcome_history_pin`) and stream it re-iterably and salt-filtered
  (`outcome_history_stream`), so replay holds one segment's decoder state
  rather than every retained row: H2's two passes read the same pinned rows and
  H3 keeps a running tally. `read_history` materializes the stream only for the
  dataset snapshot, which holds every row by definition. The archive report now
  counts the whole retained history — closed rows plus active rows — reports
  the observed span across both, and counts entries whose metadata is missing as
  `unmeasured_entries`; the plain report prints a `history:` line with the
  count, the span and that total. Regression tests cover a stream yielding
  earlier rows before a later damaged one, two passes over one stream, a
  segment replaced after the pin, an unselected domain never being decoded, the
  replay command retrying an unsettled pass but not a closed fault, append-time
  ageing against the writer's model, active rows in the report, and a missing
  sidecar counted as unmeasured.
- [x] Final gate run (2026-11-20): `make fmt` (nothing changed),
  `make check-fmt`, `make lint`, `make typecheck`, `make test`,
  `make markdownlint`, `make nixie` and `make audit` each ran once,
  sequentially, and all pass. `make lint` now executes every sub-target — ruff,
  interrogate at 100%, the base and DF12 Pylint sweeps at 10.00/10, Ambrleaks,
  Skylos 4.33.2, Vulture, the duplication gate at 253 reasoned exceptions, and
  `lint-actions`. `make test` reports 4462 passed, 85 skipped and 76 snapshots
  passed.
- [x] Verification-gap closure (2026-11-20): the follow-up assessment's three
  proof gaps are closed. The publication gate gained
  `test_a_temporary_that_does_not_match_its_facts_is_not_published`: a narrow
  seam over `read_archive_facts` reports a digest the written archive cannot
  hold, and the test proves the temporary is discarded, the fault is
  `truncated_xz`, nothing is published, the readable source survives, and an
  already-published archive and sidecar stay byte-identical. Disabling the
  `written.matches(scanned)` guard makes the test fail with "DID NOT RAISE", so
  the check is not vacuous. `test_outcome_archive_subprocess.py` runs the
  installed `build-limits-outcome-archive` script and the `python -m` form as a
  timer would: inspect, `run --dry-run`, `run`, a corrupt source (exit 3), a
  lock held by another process (exit 75), and two real runs overlapping — the
  first publishes an archive while the second reports busy and the first then
  completes. `test_outcome_archive_memory.py` traces the archiver, the
  materializing read and the streaming replay separately at 4,000 and 16,000
  rows: the archive peaks are 98.2 MB and 98.3 MB (128 MiB bound, less than 4
  MiB growth), the replay peaks are 9.25 MB and 9.42 MB (16 MiB bound), and the
  materializing read grows from 13.5 MB to 28.4 MB, which witnesses that the
  traces observe rows. Its opt-in companion,
  `BUILDLIMITS_ARCHIVE_LARGE_MEMORY=1`, generates a 320 MB history and measures
  each real process through `RUSAGE_CHILDREN`: the archiver peaked at 47,100
  KiB and the streaming replay at 47,096 KiB against a 150,000 KiB bound. It
  was run once and is skipped by default.
- [x] Gate remediation (2026-11-20): the first full gate run after the
  verification-gap closure was red in three places, and each was fixed at its
  cause. Five modules were over the 400-line limit — `outcome_replay.py` (472),
  `outcome_segment_inventory.py` (457), `outcome_history_pin.py` (413),
  `tests/test_outcome_archive_files.py` (475) and
  `tests/test_outcome_history.py` (456) — so the H2 and H3 folds moved to
  `outcome_replay_scoring`, the filesystem walk and `SegmentEntry` to
  `outcome_segment_scan`, settlement detection to `outcome_history_settle`, and
  the trim and repair tests to `test_outcome_archive_repair.py` and
  `test_outcome_history_replacement.py`. Two test-only Pylint findings were
  fixed locally, a `ty` `invalid-argument-type` diagnostic in a monkeypatched
  double was fixed by typing it as `Iterable[StoreRecord] -> H3Report`, and the
  traced memory test gained a 300-second timeout because tracing both sizes
  takes about 13 seconds alone and exceeded the 30-second default under the
  24-worker pool. A load-sensitive Hypothesis deadline in
  `test_modelcheck_trace_redaction_properties.py` failed only under the pool;
  it now disables the deadline, as the other substantive property modules do.
  The focused suites covering every touched module pass (99 passed), the traced
  memory test passes in 12.8 seconds, and the property module passes.
- [x] Second gate remediation and final gate run (2026-11-20): the gate run
  after the verification-gap closure was red in two places, and both were fixed
  at their cause. The duplicated import preamble had grown to 39 members after
  the module splits, so the three superseded entries became one entry naming
  every member; the helper extractions that followed removed the shared code
  the detector was matching, and the remaining near-duplicates are recorded as
  reasoned entries in `pyproject.toml`. The archive helper that seeds a real
  segmented store now retries a row the writer's non-blocking queue refuses:
  under load the fixture had silently written a shorter history than the test
  believed it wrote. After the remediation the eight gates ran once,
  sequentially, and all pass: `make fmt` (nothing changed), `make check-fmt`,
  `make lint` (ruff, interrogate at 100%, the base and DF12 Pylint sweeps at
  10.00/10, Ambrleaks, Skylos, Vulture, the duplication gate at 255 reasoned
  exceptions, and `lint-actions`), `make typecheck`, `make test` (4487 passed,
  86 skipped and 76 snapshots passed), `make markdownlint`, `make nixie` and
  `make audit` (no known vulnerabilities).

## Surprises & discoveries

- A module split can leave a private helper behind. `outcome_history_stream`
  imported `_fingerprint` from `outcome_history_pin`; moving that helper to
  `outcome_history_settle` broke the import at collection time. The helper is
  now shared through the settlement module instead. The same move showed that
  the settlement re-scan must be reached through the inventory module's
  attribute rather than imported by name, or a test that replaces
  `read_inventory` no longer replaces the scan the pin actually runs.
- `outcome_record_codec` already exists and houses the strict JSON Lines
  encoding; per-line decoding will be added there rather than duplicated.
- The 400-line module limit counts physical lines, so a module can pass
  `ruff` and still fail `C0302` after its docstrings and helpers grow. The fix
  is a split along an existing responsibility boundary, not a shorter
  docstring. Splitting also moves duplication families, because the detector
  compares whole units: an allow entry that named the old single module goes
  stale and the new sibling pair needs its own reasoned entry.
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
- The duplication detector's near channel clusters matching import and
  `TYPE_CHECKING` preambles into one connected family. Adding two imports to a
  single module can therefore merge two families that each had their own allow
  entry, and the merged family matches neither entry, so the gate fails without
  any new shared behaviour. The remediation added one entry naming all thirty
  members; `nose query` does not reproduce the gate's families, so the cause
  was isolated by bisecting a pristine HEAD worktree rather than by inspecting
  the detector.
- liblzma allocates its encoder dictionary through Python's allocator, so the
  benchmark's traced peak is about 98 MB at preset 6 whether it reads 500 rows
  or 523,588. The bounded-memory claim belongs to `peak_rss_bytes` (140 MB for
  a 12 MiB segment, 143 MB for a 100 MiB one), not to the traced figure; the
  "under one mebibyte" traced bound holds for the reader alone. The benchmark's
  docstrings and the developers' guide now say so.
- `StoreWriter.append` is deliberately non-blocking: a full queue refuses the
  row and publishes it as `save_failed` with `discarded=1`. A fixture that
  seeds thousands of rows through a real writer can therefore hand a test a
  shorter history than the test believes it wrote, and it does so only when the
  host is loaded enough for the worker to fall behind. The archive fixture now
  retries a refused row and fails the test beyond its bound, so a lost row is
  reported instead of silently changing what the test measures.
- An in-process RSS reading also covers the fixture. The benchmark's figures
  include the segment it generated, so they exceed what the archiver costs on
  its own: the process test measured 47,100 KiB for a 320 MB history and 47,096
  KiB for the streaming replay, well below the traced 98 MB because the
  match-finder tables are allocated but not all touched. A unit's `MemoryMax`
  must cover the process figure, not the traced one or the fixture's.
- A realistic 12 MiB segment compresses to about 6% of its size in 3.5 s, so a
  15 GB backlog takes roughly 80 minutes. A oneshot unit's default
  `TimeoutStartSec` of 90 s would kill the first run, so the documented unit
  sets two hours.
- `pytest-mock` is not installed and no test uses `mocker`, so converting the
  patch-based tests for uniformity would add a dependency; `monkeypatch` stays.

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
- When a record cap is set, it is enforced across segmented history rather than
  documented as archive-exempt: whole oldest archives are deleted while they
  fit the excess, then the straddling archive is trimmed. The default policy
  leaves the cap unset, so the 15,000,000,000-byte policy is unaffected.
- The 15,000,000,000-byte budget and the timer are not provisioned on the
  managed host by this change. The guide names
  `cache_outcomes_max_bytes = 15000000000` and says that permanent provisioning
  is outside this release.
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
- The store reader and the segment reader disagree about a decodable final row
  without a newline: `read_store` cuts at the last newline, while the archive
  reader keeps a fragment that decodes. Both are deliberate. The active file is
  still being appended to, so the writer repairs it to the decoder's view; a
  sealed segment is closed, and a complete row whose newline was lost is
  history that must not be dropped. The developers' guide states both rules.
- Retention planning uses an archive's physical size when its sidecar is
  untrustworthy. For XZ that figure is a lower bound, not a safe overestimate,
  so any run that repaired a sidecar now re-plans from the repaired inventory
  instead of relying on the first plan.
- A byte trim and an age trim compose into one rewrite, but a sequence the byte
  plan deletes is excluded from the trim list: rewriting a file that is about
  to be removed cannot improve the retained bytes.
- A history read pins a snapshot rather than re-reading the store per pass:
  device, inode and size fingerprints detect a replacement between passes, the
  active file's bytes are frozen at the pin, and a pass whose pin changed fails
  closed as `unsettled` rather than mixing two states. The replay command takes
  a fresh pin after such a fault; a closed decode fault is never retried.
- The report's record count covers the whole retained history, including the
  writer's active rows, and `unmeasured_entries` counts segments no metadata
  describes. An unknown part of the history is never reported as empty, and the
  observed span is the union of the measured closed and active spans.

- Sealed sources are not metadata-free: the writer writes
  `segment-NNNNNNNNNNNN.jsonl.meta.json` when it seals, so a sealed entry's
  record count and span are known to the inventory. An archive whose sidecar is
  missing or corrupt, and an active file that cannot be decoded, are the only
  entries the report has to call unmeasured.
- Test fixtures that seed a real segmented store accept a row only when the
  writer reports it, retrying a refused append. Treating `append` as
  synchronous would hide the writer's documented loss policy and let a loaded
  host shrink the fixture's history.

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
- A publication-gate regression test in `tests/test_outcome_archive_files.py`
  that injects mismatched archive facts through a narrow seam and proves
  nothing is published, and process-level tests in
  `tests/test_outcome_archive_subprocess.py` that run the installed command and
  two overlapping runs.
- Fixed `tracemalloc` bounds at two history sizes
  (`tests/test_outcome_archive_memory.py`, 4,000 and 16,000 rows), plus an
  opt-in subprocess test (`BUILDLIMITS_ARCHIVE_LARGE_MEMORY=1`) with a
  320,000,000-byte history and a 150,000 KiB `ru_maxrss` bound, with the
  archiver and the replay consumer each measured in their own process.
- Boundary tests at exactly 30 days and exactly the byte cap, including sparse
  history below both bounds and an explicit record cap.
- A benchmark target reporting compression ratio, elapsed time, child peak RSS
  and peak temporary disk use on generated representative data.

Non-vacuity: every boundary test exhibits at least one record on each side of
the threshold; the memory tests compare two sizes so a constant-buffer failure
or an accumulating buffer both fail, while the materializing read's measured
growth proves the traces observe rows; and disabling the publication gate makes
`test_a_temporary_that_does_not_match_its_facts_is_not_published` fail.

## Revision note

- Initial draft created while implementing issue #63.
- Review remediation added the CodeRabbit fixes, the new boundary tests and the
  `RLIM_INFINITY` portability fix for `tests/test_harness_ownership.py`; the
  decision log's "report rather than change" entry was replaced accordingly.
- The second review remediation added thirteen fixes with one regression test
  each, two new test modules for the tail repair and sealing bounds, the
  `gaps_truncated` field documented in ADR 0010, and the two documentation
  corrections: the timer unit's store path, and the torn-tail rule as the
  readers actually implement it.
- The assessment remediation added the eight correctness fixes, the generated
  archive-and-replay test, the benchmark's time, RSS and scratch fields, the
  timer unit's resource limits, and the guide's sizing table. The benchmark
  figures come from a 12 MiB and a 100 MiB generated segment measured on the
  development host at preset 6; the docstrings and developers' guide were
  corrected to attribute the traced peak to the encoder dictionary.
- The coverage lift added five focused test modules, one shared injected-failure
  helper, and two reasoned duplication exceptions, and records the final
  all-green gate run.
- The whole-plan remediation streamed the retained history through a pinned
  snapshot, made the archive report count the active file as well as the closed
  segments, and added the publication-gate, process-level and two-size memory
  tests, the opt-in 320 MB process test, and the developers' guide note that
  explains how to run it.
- The second gate remediation split the replay, pin, settlement and inventory
  modules along their responsibility boundaries, recorded the merged
  duplication families, and made the archive fixture retry a row the writer's
  non-blocking queue refuses; the plan records the final all-green gate run.
