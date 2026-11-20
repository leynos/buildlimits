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
  sidecar and the archive directory are durable. The one exception is a segment
  wholly outside the retention bounds: retention evicts such a segment without
  compressing it, because archiving history the same plan would delete spends
  the scratch headroom for nothing. A partially expired segment is still
  archived first, and its archive is then trimmed.
- All archive work is streamed in fixed-size chunks, so the encoder and decoder
  buffers depend on the XZ preset and the chunk size rather than on retained
  history. The inventory, the plan and the report hold one small record per
  segment, so the peak also grows by a bounded amount per segment. No code path
  buffers a segment's rows.
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
- [x] Third remediation: the whole-plan assessment's remaining four defects
  (2026-11-20). The record cap now counts the active file and evicts the oldest
  closed history first, deleting a sealed source whole when the budget would
  remove its rows anyway rather than compressing and then deleting it; the
  archive run-fault tests were rewritten to that contract. H2's runtime
  baseline keeps a fixed 64-hit window per paired key, so a hot key cannot grow
  the fold with the history; the behavioural test watches the window roll and
  the memory test scores 100,000 streamed hits under one mebibyte. An idle
  writer expires aged rows through its idle tick and reports the tick as
  `idle_tick_seconds`, and record-cap sealing is exercised in segmented mode. A
  filesystem that fills after the headroom check now records `no_headroom` per
  source and exits 75, and an end-to-end test shows the next firing completes
  the pass once space returns. Finally, the bounded private-file reader that
  finding 2 introduced now allocates the descriptor's reported size plus one
  byte instead of the whole 16 MiB bound, follows concurrent growth in 64 KiB
  chunks, and refuses a file only once it passes the bound. The targeted suite
  passes: 301 passed and 1 skipped across the archive, retention, replay,
  history, store, segment-inventory and memory modules, including the 6,000-row
  generated archive-and-replay memory test that the first reader change had
  broken.
- [x] Fourth gate remediation (2026-11-20): the gate run over the third
  remediation cleared `check-fmt`, `typecheck` (with one load-sensitive
  `test_outcome_loop.py` failure that passed in the earlier run and in
  isolation), `markdownlint`, `nixie` and `audit`, and found three defects in
  `make lint`. Four ruff violations in the new H2 test helper were fixed
  directly. Two test modules still exceeded the 400-line limit, so the reader
  tests moved to `tests/test_outcome_store_reader.py` and the H2 runtime-window
  tests to `tests/test_outcome_replay_h2_window.py`, with the row builders
  shared from `tests/outcome_replay_h2_helpers.py`. The duplication gate, newly
  reached, reported six families: the two budget walks were extracted into one
  helper, and the remaining five families are recorded as reasoned exceptions
  (260 in total), with the two superseded prelude entries replaced by one
  forty-member entry. The focused suites pass (39 store/reader tests, 19 H2
  tests, 53 archive planning tests, and the isolated loop test), and the
  duplication gate passes at 260 reasoned exceptions.
- [x] Fifth fix round (2026-11-20): the next gate run cleared `check-fmt`,
  `typecheck`, `test` (4504 passed, 86 skipped; the loop test passed), and the
  documentation gates, and failed `make lint` in `ruff check` with four
  findings on the new budget helper: six parameters on `_evict_oldest` (Ruff
  allows four) and missing `Returns` sections on the two trim functions. The
  walk became a small `_BudgetWalk` dataclass whose `surviving` and `evict`
  methods own the candidate list and the two decision maps, so the method takes
  three arguments and needs no suppression; the trim docstrings gained their
  return descriptions. `ruff check`, `make typecheck` and the 36 planning and
  retention tests pass on the result.
- [x] Sixth fix round (2026-11-20): the whole-plan assessment's six remaining
  defects are closed, each with a regression test that fails on the previous
  code. `open_private_regular` adds `O_NOFOLLOW` and `O_NONBLOCK` itself, so no
  caller can lose the no-follow rule, and `active_history` refuses an active
  path that is not a regular file rather than reading whatever a symlink names.
  Eviction builds one sequence-sorted walk of trims and deletions and stops at
  the first failure, so a newer trim can no longer follow a failed older
  deletion. A composed age-and-byte trim measures every policy drop over the
  whole source (`Trim.policy_bytes`) before the byte suffix decides, so a row
  that expires later in the document cannot cost an earlier eligible row its
  place. The retention floor advances only over a contiguous deleted prefix, so
  a sequence deleted above an unknown gap leaves the gap reported. The writer
  checks the record-cap `settle()` result and refuses the row when the seal
  failed, because the archiver cannot evict active rows. The users' guide now
  says the archiver reads the active file once per plan and never writes or
  replaces it. The two composed-trim tests prove the measurement at the encoder
  and through `recompress_archive`; the active-file and opener tests drive a
  real symlink; and the cap-seal test follows the refusal with an archive run
  that retains exactly the cap. The six touched modules pass (66 tests).
- [x] Gate remediation (2026-11-20): the first full gate run after the sixth
  fix round was red at `make lint`'s duplication sub-gate with six unsuppressed
  families, and `lint-actions` never ran because `make` stopped there. Two
  families were real copies and are now one implementation each: the archiver's
  `open_private_regular` delegates to the outcome store's primitive, which
  gains a `validate` seam so the archiver keeps its ownership requirement while
  the no-follow, non-blocking flags and the close-on-failure rule live in one
  place; and `outcome_archive_rows.iter_records` owns the record walk and its
  stream-to-segment fault translation, used by `scan_archive` and
  `policy_dropped_bytes`. The other four families are import preludes shared by
  suites that drive different fixtures, and one property proved twice at two
  public boundaries; each is recorded as a reasoned exception. The entry the
  consolidation made obsolete was removed. Focused checks pass: ruff, both
  pylint passes, interrogate, ty, and 271 archive and store tests.
- [x] Final gate verification (2026-11-20): the full sequence passed on the
  remediated tree. `check-fmt`, `lint` (including `lint-actions`), `typecheck`,
  `markdownlint`, `nixie` and `audit` were green in one run; the `make test`
  re-run then passed with 4,513 tests and 86 skips. The first `make test` run
  had failed only on the two doctests whose example now names the typing-only
  `LineStream`, because a doctest executes in the module's globals; each
  Example block imports that name explicitly.
- [x] Migration-documentation remediation (2026-11-20): the 0.2.0 migration
  guide now signposts the archiver and the segmented store. The opening
  paragraph names `build-limits-outcome-archive` and states that the archive
  keys are new and optional; Table 1 gains configuration, command-line, make
  target and store-layout rows; a new upgrade step sets the history budget
  before the timer; the reset procedure covers `cache-outcomes.seq`, the sealed
  directory and the archive directory; and `docs/contents.md` lists the guide's
  new subject. The guide also no longer implies the journal is the only change
  an operator sees without opting in: sealing begins only when the history
  budget is raised above its 12 MiB default.
- [x] Whole-plan revision-12 remediation (2026-11-20): a count trim that would
  compose with a time or byte trim is deferred to a follow-up plan pass, so the
  other trim is applied and measured before the count is decided and no
  eligible row is discarded; the follow-up loop is bounded at three passes. The
  active path is refused unless it is absent or a regular file, and a record
  cap with unreadable active rows reports `failed`/`unreadable` at exit 1.
  Constraint 2 and invariant 3 now permit whole eviction of a sealed source
  outside the bounds, and the users' and migration guides describe that
  eviction, the active-file refusal and `max_bytes = 0` segmentation. The
  bounded active-file read moved to a new `outcome_archive_active` module, and
  the new regression tests live in `test_outcome_archive_active` and
  `test_outcome_archive_straddling`, so every touched module stays under the
  400-line limit. Two complete-run regression tests pin the composed retention,
  and the focused archive suites pass. The first gate run then found
  duplication in the new tests: the repeated refusal assertions became one
  `_assert_refused` helper, and the import-list and fixture-shape overlaps with
  the existing archive suites are recorded as reasoned `df12-duplication`
  exceptions.
- [x] Whole-plan revision-13 remediation (2026-11-20): whole-segment budget
  deletion, and every byte or record trim, now wait until the age outcome is
  settled and no duplicated sequence is excluded from the candidates, because
  both walks act on totals that still include rows a pending age trim removes;
  age eviction is still planned from the cutoff alone, and the bounded
  follow-up pass re-plans from the reconciled and measured inventory. A pass
  that defers the walk still reports a configured but unmeasurable record cap.
  `age_trims_are_unsettled` moved beside the walk it guards, keeping
  `outcome_archive_plan` and `outcome_archive_budget` under 400 lines. Three
  complete-run regressions pin the behaviour: a record cap and a byte budget
  across two segments with a straddling age trim, and a duplicate pair whose
  older deletion fails while the newer archive stays byte-identical. The two
  planner tests that pinned same-pass age-and-byte composition now pin the
  deferral, and the users' guide states that one firing may apply retention
  over more than one pass. The focused duplication gate then found the two new
  segment tests sharing one fixture outline and two import-prelude families
  changed by the new imports: the fixture became one `_write_eviction_fixture`
  helper, and the prelude families are recorded as reasoned `df12-duplication`
  exceptions, including the plan module's typing-only prelude in the
  repository-wide entry.
- [x] Whole-plan revision-14 remediation (2026-11-20): the byte and record
  walks now also wait while any surviving archive has no trustworthy sidecar,
  because the physical-size fallback is not a lower bound on the history the
  file holds; the heal step and the follow-up pass measure it. A configured
  budget left to the follow-up is reported as `plan.retention_deferred` in
  `inspect` and `run --dry-run`, both as a JSON field and as a prose line, and
  a complete-run regression and two planner tests pin it. The bounded-memory
  claim now names the per-segment bookkeeping, and a new measurement of the
  same 16,000-row history in 18 and 74 segments bounds that share at about 4
  KiB per segment. The first lint run found one C1803 empty-tuple comparison in
  the new retention assertion; after that fix all seven gates pass once each,
  with `make test` reporting 4526 passed and 86 skipped, 76 snapshots, and the
  duplication gate allowing 267 reasoned exceptions.

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
- The headroom check reads free space once and reserves nothing, so another
  writer can fill the filesystem while the pass runs. A write that met `ENOSPC`
  was recorded as `unreadable` and reported as `corrupt`, which told the timer
  the history was damaged instead of deferring the work. A full filesystem is
  now `no_headroom`, and a run whose every failure was `no_headroom` exits 75;
  the users' guide, ADR 0010's exit table and the recovery test all state that
  the check is not a reservation.
- A buffered `read(n)` allocates `n` bytes even when the file is smaller.
  Reading `limit + 1` for a 6 KiB active file cost 16.9 MB traced and broke the
  generated history test's memory contract, so `read_private_bytes` asks for
  the descriptor's reported size plus one byte and follows any growth in 64 KiB
  chunks. The same file now costs about 138 KiB traced, and a concurrent append
  is still tolerated up to the bound instead of being reported as `corrupt`.
- Adding modules can break the duplication gate without adding duplication. The
  detector pools every module's import and `TYPE_CHECKING` prelude into one
  near-matched family, so the two new test helper modules and the reader split
  merged families that each had their own reasoned exception into one 40-member
  family that matched neither. The gate then reported a value of 288.9 with no
  new shared behaviour to remove. The remediation replaced the narrowed entries
  with one entry naming all forty members, and the gate now passes at 260
  exceptions. Treat a family that suddenly grows after a file move as a
  regrouping signal first, and only then as evidence of real duplication.
- One full gate run failed only
  `tests/test_outcome_loop.py::test_loop_admits_while_query_stalls`, which
  asserts that a timed-out helper is cancelled before its result arrives. The
  file is untouched by this branch, the test passed in the previous full run,
  and it passed alone in 0.20 s, so the failure is a timing flake under xdist
  rather than a regression this work introduced. The final gate run is the
  confirmation: if it recurs, the assertion needs a wider bound instead of a
  code change here.
- `os.open` follows a symlink unless `O_NOFOLLOW` is passed, and a flag each
  caller had to remember was one omission away from reading a file the store
  never owned. The opener now adds the flag itself; the archiver's active-file
  read additionally requires a regular file through `lstat`, because no-follow
  alone still accepts a hard link or a regular file that merely sits at the
  path.
- Eviction ordered all trims before all deletions. A record cap could therefore
  trim the next archive after the older whole deletion had failed, discarding
  rows from history that survives while the entry the budget asked for was
  still present. A single sequence-sorted walk that stops at the first failure
  is the only ordering that keeps "oldest first" true across both operations;
  the follow-up `retain` pass then re-attempts the failed deletion before any
  trim.
- Counting policy drops only as they are met over-drops whenever an expired row
  follows the first eligible one: the eligible row is judged against a document
  that still contains bytes the policy will remove. Only a pass over the whole
  source can say how many bytes the age rule drops, so `policy_dropped_bytes`
  measures it before the byte suffix is decided. The measurement rides on
  `Trim.policy_bytes` rather than a new `write_archive` parameter, which keeps
  the call inside Ruff's four-argument bound.
- A single-integer retention floor cannot both hide retention's deletions and
  remember an unknown gap. Advancing the floor over every deleted sequence
  hides the gap from the next scan; advancing only over the contiguous deleted
  prefix preserves it, and a sequence retention deleted above the gap is then
  reported as a gap itself. The conservative report is the accepted trade-off.
- `StoreWriter.append` ignored `settle()`'s result at the record cap. A seal
  that failed before its rename leaves the rows in the active file, which the
  archiver cannot evict, so accepting the next row held history past the cap
  that no run could remove. The refusal reports `save_failed` with one
  discarded row, and the next successful seal returns the store to its bound.
- The guide described the archiver as owning the active file's age bound while
  also promising never to touch the active file. The archiver does read the
  active file once per plan, through the bounded no-follow reader, to count the
  rows and span the writer currently holds; it never writes or replaces it. The
  guide now states both halves.
- A compressed archive's file size is not a lower bound on the logical history
  it holds. The XZ container adds its own header and index overhead, so a
  header-only document of 107 logical bytes occupies 156 bytes on disk; a
  budget planned from the physical-size fallback can therefore overstate the
  store, not only understate it. The missing-sidecar deferral and the
  `_archive_entry` docstring were corrected for this.

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
  command reading only the active store, so an existing invocation reads the
  same rows it read before. That is a statement about the input, not about the
  H2 classifier: since revision 9 the runtime clue compares a row with the
  median of the key's most recent 64 earlier hits on every path, including a
  replay with no flag and a report built from the active store alone. This is
  an approved classifier change, required by the bounded-memory constraint, and
  its compatibility impact is that a history whose key has more than 64 earlier
  hits can produce a different `runtime_correct` and combined agreement, and so
  a different H2 verdict, than an earlier release did. Every reported field is
  unchanged and the report schema stays 1; no stored row changes shape and no
  migration is needed. Both commands share the option definitions in
  `outcome_history_args`.
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
  untrustworthy. That figure does not describe the logical bytes: the container
  header can make a small archive's file larger than its content, so the
  fallback can overstate the store as well as understate it. A byte or record
  walk is deferred while such a sidecar survives, and any run that repaired a
  sidecar re-plans from the repaired inventory instead of relying on the first
  plan.
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
- With a record cap set, the planner evicts the oldest sealed sources whole
  before compressing anything the cap would remove. The cap counts the writer's
  active rows, so the closed history can already be over budget before the
  first archive exists; archiving a source only to delete its archive would
  spend the scratch headroom for nothing.
- H2's runtime baseline uses a fixed window of 64 earlier hits per paired key.
  Keeping every hit made the fold grow with the history and made the per-row
  median quadratic; the window bounds both while still comparing a row with the
  key's recent behaviour.
- `read_private_bytes` reads the descriptor-reported size plus one byte, then
  follows growth in 64 KiB chunks. Asking for the caller's limit allocated the
  whole bound for a small active file, and refusing any growth would turn a
  writer's concurrent append into a `corrupt` history fault.
- The two budget planners now share one `_BudgetWalk.evict` walk. Their
  delete, sealed-source and straddling-trim control flow was identical, and
  keeping two copies risked one retention rule drifting from the other; only
  the measure (`planned_size` or `planned_records`) and the straddling entry's
  trim differ, and the walk takes both as arguments. The record walk's `None`
  count still ends the walk, through a `keep` callable that reports "cannot be
  planned". The candidates and the two decision maps travel as one object
  because every step of the walk reads and writes all three, which also keeps
  the method inside Ruff's four-argument bound instead of needing a suppression.
- The duplication gate's families are regrouped by the detector as the tree
  changes, so an exception that names a subset stops matching once the family
  grows: the prelude family was reported with 40 members while two older
  entries named 5 and 7 of them. Both stale entries were replaced by one entry
  naming all 40 locations, and the remaining new families are recorded as
  reasoned exceptions rather than force-extracted: the store tests' import
  lists, two production import preludes, and two retention test bodies that
  share the suite's fixture shape while proving different rules.
- A private opener owns the no-follow rule. `open_private_regular` adds
  `O_NOFOLLOW` and `O_NONBLOCK` to every call, because a safety rule that each
  caller must remember is one new caller away from being lost; a caller may
  still pass the flags redundantly. The archiver's active-file read goes
  further and refuses any path that is not a regular file before reading it,
  since a store's history is only ever the writer's own file.
- Eviction is one sequence-ordered walk over trims and whole deletions, ending
  at the first failure. The oldest entry the budget removes must be gone before
  any newer entry is rewritten, or the newer trim spends bytes that the failed
  deletion still needs to account for.
- `Trim.policy_bytes` carries the bytes the age rule drops over the whole
  source, measured by `policy_dropped_bytes` before the byte suffix is chosen.
  The byte allowance is compared with the document that survives every policy
  drop, not with the rows met so far; a zero value means unmeasured and
  over-drops rather than exceeding the allowance. Keeping the measurement on
  `Trim` avoids a fifth `write_archive` argument.
- The retention floor advances only over a contiguous prefix of deleted
  sequences, starting from the previous floor or the lowest sequence seen. A
  deletion above an unknown gap cannot raise the floor past it; reporting that
  deleted sequence as a gap is the deliberate cost of a single-integer floor.
- When the record-cap seal fails, the writer refuses the row instead of
  appending past the cap: the archiver cannot evict active rows, so accepting
  it would retain history nothing could remove. The refusal is a `save_failed`
  append with one discarded row, not a store fault.
- A count trim is deferred, not composed, when the straddling archive already
  carries a time or byte trim. The count is a prefix over the document's row
  positions, so it cannot target the rows a time or byte rule removes at other
  positions: a document ordered *expired, at cutoff, expired, fresh*, capped at
  two rows, keeps the two eligible rows once the expired pair leaves, while a
  composed trim would drop the row at the cutoff too. The walk therefore stops
  at such a straddler, and `retain()` plans again over the rewritten document
  so the count is decided from the sidecar's measured row count. The follow-up
  loop is bounded at three passes, and a plan proposing a trim this run already
  applied is ignored rather than repeated.
- Only an absent active file is empty history. A path that exists but is not a
  regular file raises `UnsafePathError`, and any other failure to inspect it
  propagates, so a substituted or uninspectable path cannot make the store look
  empty to either budget. A file that exists but cannot be decoded is reported
  by size with `has_metadata=False`; if a record cap is configured, planning
  raises `UnknownActiveHistoryError` because the cap cannot be shown to hold,
  and the run reports `failed` with exit 1. A byte-only run may still proceed,
  because it measures the active file by `stat`.
- Constraint 2 and invariant 3 now state the sealed-source eviction the
  planner already implements: a segment wholly outside the bounds is removed
  without being compressed, while a partially expired segment is archived first
  and its archive trimmed. The users' guide and the 0.2.0 migration guide say
  the same, and the migration guide now names `max_bytes = 0` as the other way
  to turn on the segmented store.
- The byte and record walks are deferred whenever this pass's totals are
  untrustworthy: a duplicated sequence is excluded from the candidates while
  reconciliation settles it, or a surviving entry's span crosses the age
  cutoff. A whole deletion chosen against such a total can remove an older
  archive the settled store keeps, because `planned_size` and `planned_records`
  still count the rows an age trim is about to drop. Age eviction is decided
  from the cutoff alone and is planned either way; `retain()` re-plans from the
  reconciled and measured inventory, and a pass that defers its walk still
  reports a configured but unmeasurable record cap.
- A byte or record walk is also deferred while any surviving archive lacks a
  trustworthy sidecar. The fallback counts the XZ file's own size, and a
  container header makes a small file larger than the history it holds, so that
  figure can overstate the store as well as understate it: a whole older
  deletion chosen against it would discard history the settled store keeps. The
  heal step repairs the sidecar, and the follow-up plan measures it.
- `Plan.retention_deferred` names the work the first plan leaves to the
  follow-up. `inspect` and `run --dry-run` must not present an empty trim and
  delete count as final when a configured budget could not be decided yet. The
  flag is set only when a byte limit or record cap is actually configured, so a
  policy with no bound reports no deferral it never had.
- The memory claim is stated as a fixed streaming cost plus per-segment
  bookkeeping rather than as an absolute constant. Compression and verification
  never buffer a segment, but the scan builds one entry per segment, the plan
  holds one per action and the report names each performed action, so those
  allocations are O(segments). The claim now says so, and the segment-count
  measurement bounds the share at about 4 KiB per segment instead of asserting
  a segment independence the code does not provide.

## Outcomes & retrospective

The timer-facing archive command, its verification and retention execution, the
writer-owned sealing and the archive-aware replay and dataset paths are
implemented and covered by dedicated modules across the archive, segment and
history suites. The command keeps paths out of its successful output, maps
every outcome to the documented exit status, and was checked against the real
system clock only in the redacted CLI snapshot.

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
3. An uncompressed source is removed only after a decompressed archive matches
   its record count, logical bytes and SHA-256, or because retention evicted
   the whole segment as outside the bounds; an evicted segment therefore has no
   archive to match.
4. Retained logical bytes never exceed the configured budget after a successful
   run unless one active segment plus one record transiently exceeds it.
5. Eviction is oldest-first; records at the cutoff survive.
6. Iterating history yields records in sequence order: archived or sealed
   segments ascending, then the active file.
7. Peak memory during archiving is the XZ encoder's working set, fixed by the
   chunk size and preset, plus an allocation per segment the inventory, plan
   and report describe. No archive, trim or eviction buffers a segment's rows,
   so the peak does not follow the retained history's size; the per-segment
   share is measured at about 4 KiB.

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
  (`tests/test_outcome_archive_memory.py`, 4,000 and 16,000 rows), plus a third
  measurement of the same 16,000-row history sealed into quarter-size segments:
  74 segments peaked at 98,454,136 traced bytes against 18 segments at
  98,215,635, so 56 extra segments cost 238,501 bytes, about 4 KiB each, which
  is the per-segment bound invariant 7 states. An opt-in subprocess test
  (`BUILDLIMITS_ARCHIVE_LARGE_MEMORY=1`) with a 320,000,000-byte history and a
  150,000 KiB `ru_maxrss` bound measures the archiver and the replay consumer
  in their own processes. H2's fold is measured separately: 100,000 streamed
  hits for one hot key must stay under one mebibyte, which the unbounded fold
  failed by timing out after its per-row median became quadratic.
- Boundary tests at exactly 30 days and exactly the byte cap, including sparse
  history below both bounds and an explicit record cap, plus device-level tests
  that a store which grew after its size check is followed up to the bound and
  refused past it.
- Recovery tests for a filesystem that fills after the headroom check: every
  source it could not hold is recorded as `no_headroom`, the run exits 75, no
  source is lost, and an unpatched second run archives both sources.
- A benchmark target reporting compression ratio, elapsed time, child peak RSS
  and peak temporary disk use on generated representative data.

Non-vacuity: every boundary test exhibits at least one record on each side of
the threshold; the memory tests compare two sizes so a constant-buffer failure
or an accumulating buffer both fail, while the materializing read's measured
growth proves the traces observe rows; the segment-count test asserts its
fixture really sealed more than three times the segments before comparing
peaks, so a writer that ignored the segment size could not make it pass
vacuously; and disabling the publication gate makes
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
- The third whole-plan remediation closed the seven correctness defects:
  replay rechecks each opened segment's identity against its fingerprint, the
  active file is read through the bounded no-follow reader, sealed sources are
  evicted oldest-first together with archives, the writer seals at the record
  cap and compacts an idle store past the age cutoff, H2 keeps a 64-hit runtime
  window per paired key, and the headroom setting is documented as a check
  rather than a reservation. The budget planners share one `_BudgetWalk`, the
  H2 and reader tests moved to focused modules, and the plan records the final
  all-green gate run.
- Revision 8 records the remaining whole-plan remediation of findings 4 to 7:
  the record cap counts the active file and evicts sealed sources whole, H2
  keeps a bounded recent-hit window, record-cap sealing and idle expiry are
  exercised in segmented mode, the headroom check is documented as a read
  rather than a reservation with `no_headroom`/`EX_TEMPFAIL` recovery, and the
  bounded private-file reader follows concurrent growth without allocating its
  caller's limit.
- Revision 9 records the retention-and-symlink remediation of the whole-plan
  assessment's six defects: the private opener adds `O_NOFOLLOW` and
  `O_NONBLOCK` itself and the archiver refuses a non-regular active path,
  eviction interleaves trims and deletions in sequence order and stops at the
  first failure, a composed age-and-byte trim measures every policy drop before
  the byte suffix, the retention floor advances only over a contiguous deleted
  prefix, the writer refuses a row when the record-cap seal fails, and the
  guide states that the archiver reads the active file once per plan without
  writing it. Each fix carries a regression test; the six touched modules pass
  (66 tests).
- Revision 10 records the duplication-gate remediation that followed revision
  9's first full gate run. One private open path now serves both the archiver
  and the outcome store, parameterized by the guarantee the caller requires, so
  the no-follow rule and the mode narrowing cannot drift between them; one
  record walk, `iter_records`, owns the stream-to-segment fault translation,
  and the archiver's verification and trim measurement both use it. Four
  families remain as reasoned exceptions: import preludes in suites that drive
  different fixtures, and one composition property proved at two public
  boundaries. The exception the consolidation made obsolete was removed. The
  final gate run passed all seven gates, with 4,513 tests passed and 86
  skipped, after the two doctest Examples gained their `LineStream` import.
- Revision 11 records the migration-documentation remediation: the 0.2.0
  guide now names the archiver and the segmented store in its introduction,
  Table 1 and a new upgrade step, gives the reset procedure for a store with
  segments, and is described accordingly in `docs/contents.md`.
- Revision 12 records the whole-plan revision-11 assessment's four findings.
  A count trim no longer composes with a time or byte trim: the walk stops at
  such a straddler and `retain()` plans again after the other trim is applied,
  so the count is decided over the rows that actually survive. Only an absent
  active file is empty history; a non-regular path raises `UnsafePathError`,
  other inspection failures propagate, and a record cap over undecodable rows
  raises `UnknownActiveHistoryError`, all reported as `failed`/`unreadable` at
  exit 1. Constraint 2 and invariant 3 now state the sealed-source eviction the
  planner already performs, and the users' and migration guides describe the
  eviction, the active-file refusal, and `max_bytes = 0` segmentation. The
  active-file read moved to `outcome_archive_active`, and the new regression
  suites split into `test_outcome_archive_active` and
  `test_outcome_archive_straddling`, keeping every module under 400 lines.
- Revision 13 records the whole-plan revision-12 assessment's two findings.
  The byte and record walks no longer choose whole deletions, or any trim,
  while a surviving entry straddles the age cutoff or a duplicated sequence is
  excluded from the candidates: their totals would omit rows that are still
  retained. Age eviction is planned from the cutoff alone, and the bounded
  follow-up pass re-plans the budgets from the reconciled and measured
  inventory; a pass that defers the walk still reports an unmeasurable record
  cap. `age_trims_are_unsettled` now lives with the walk it guards in
  `outcome_archive_budget`. New complete-run regressions cover a record cap
  across two segments, a byte budget across two segments, and a duplicate pair
  whose failed older deletion leaves the newer archive byte-identical; the
  planner tests now pin the deferral rather than same-pass composition.
- Revision 14 records the whole-plan revision-13 assessment's three findings.
  The budget walks also wait while a surviving archive has no trustworthy
  sidecar, because a container header can make the XZ file larger than its
  content and the physical-size fallback therefore overstates as well as
  understates the store; a complete-run regression proves neither archive is
  rewritten once the healed store fits. `inspect` and `run --dry-run` now report
  `plan.retention_deferred`, with a prose line, when a configured budget is
  decided by the follow-up pass, and a CLI regression proves the reported
  zeroes give way to a deletion and a trim in the run that settles the
  duplicate. The bounded-memory claim is restated as a fixed streaming cost
  plus a measured per-segment share, and the segment-count measurement bounds
  that share at about 4 KiB per segment for 18 and 74 segments of the same
  history.
