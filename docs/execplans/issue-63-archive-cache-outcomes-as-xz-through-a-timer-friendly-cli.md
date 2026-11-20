# Archive cache outcomes as XZ through a timer-friendly CLI

Status: COMPLETE

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
- [x] Whole-plan revision-15 remediation (2026-11-20): the budget walks also
  wait while a surviving sealed source has no writer metadata, because its
  exact bytes still carry no span and the plan cannot see the age eviction that
  removes it; the walk now reports whether it settled the budget, and a stopped
  walk defers the configured budget in `inspect` and `run --dry-run`. A
  complete-run regression keeps the older archive byte-identical while the
  newer unmeasured source is archived, measured and age-evicted, and a CLI
  regression covers a measured sealed source straddling a byte budget. The
  suite run also caught a control-flow defect in the rewritten walk, where an
  exact-fit deletion went on to trim the next archive; the existing planner
  tests pin the corrected behaviour. All seven commit gates pass once each on
  the final tree.
- [x] Whole-plan revision-16 remediation (2026-11-20): the writer never takes
  the archive lock, so it can seal a source after the run has planned. Each of
  the bounded follow-up passes now re-reads the store and performs the archive,
  reconcile, heal and orphan work it discovers before planning the budget over
  the settled inventory, so a source that arrives mid-run is still archived,
  measured and capped in the same firing. The follow-up loop moved to
  `outcome_archive_retain`, which narrows every pass to work the run has not
  already applied and leaves a failed eviction, and every newer one, for the
  next firing; work still pending after three passes reports the new retryable
  `unsettled` status at exit 75 instead of `ok`. Two deterministic
  writer/archiver interleaving regressions pin a mid-run seal being archived
  and capped at `retained_records == 5` with status `ok`, and a seal on every
  pass reporting `unsettled` at exit 75, which the next run settles. The
  planner's retention contract also gained property coverage: two Hypothesis
  properties over generated measured histories assert that eviction is a
  whole-entry oldest-first prefix, at most one straddler is rewritten, and the
  planned byte budget and record cap both fit. All seven commit gates pass once
  each on the final tree, with `make test` reporting 4532 passed and 86
  skipped, 76 snapshots, and the duplication gate allowing 269 reasoned
  exceptions.
- [x] Whole-plan revision-17 remediation (2026-11-20): four settlement defects
  are fixed. A trim is suppressed only when both the trim and the archive
  measurement match the one it was applied to, so the same allowance proposed
  for a re-measured archive - the active file grew past the cap after the
  rewrite - is applied again instead of leaving the store over its bound while
  the run reports `ok`. Each pass reads the inventory, the active file and the
  inventory again, and abandons the pass for a fresh pair of reads when a seal
  moved rows between the two scans. A pass that quarantines an archive or a
  sealed source stops before eviction, because the removed rows were part of
  the total the budget was planned over. A non-mutating plan after the last
  permitted pass distinguishes a settled store from an exhausted loop, so work
  the third pass finished reports `ok`. Four complete-run regressions live in
  the new `test_outcome_archive_settlement`, and the planner's age rule and
  active-row cap each gained a Hypothesis property with an executable negative
  control. All seven commit gates pass once each on the final tree, with
  `make test` reporting 4538 passed and 86 skipped, 76 snapshots, and the
  duplication gate allowing 272 reasoned exceptions.
- [x] Whole-plan revision-18 remediation (2026-11-20): three active-history
  defects are fixed. Both budgets now count the active rows the age rule keeps
  rather than the whole file: a row the writer will compact away is not
  retained history, and counting it evicted an archive that fitted its bound
  while leaving the eligible archive byte-identical only when the expired rows
  were excluded. The active file is opened once and measured through that
  descriptor, so its size and rows cannot come from two versions when the
  writer replaces the path mid-scan, and the cutoff scan reports the surviving
  bytes and rows beside the whole-file figures. The final report reads the
  inventory on both sides of the active read and retries until they agree, so a
  seal cannot leave either copy out of the reported count and span; a writer
  that seals on every reading reports `unsettled` at exit 75. Four complete-run
  regressions live in the new `test_outcome_archive_active_budget`, two in
  `test_outcome_archive_active`, and two in `test_outcome_archive_settlement`.
  Each fix was mutated back to its pre-fix behaviour and the matching
  regressions failed for the intended reason before the mutation was reverted.
  The 400-line module ceiling forced two structural extractions:
  `outcome_archive_age` owns the age decisions, and `outcome_archive_history`
  owns the active snapshot and the retained-history summary.
- [x] Whole-plan revision-19 remediation (2026-11-20): four measurement defects
  are fixed. A final row whose newline is not written is the writer's append in
  flight, which a failed write truncates back out, so it is excluded from both
  the whole-file and the surviving counts instead of evicting an archive the
  committed rows fit beside. An active file that cannot be decoded no longer
  lets an age-aware byte budget evict against bytes retention may have already
  released: the walk is deferred and the run reports `unsettled` at exit 75.
  The active scan reads the size immediately before the rows and confirms it
  immediately after, retrying a version the writer appended to, so a growing
  file cannot pair its later rows with the earlier size; a complete-run
  regression appends a row between the size reading and the scan, then checks
  the inspection's planned eviction and the run's applied eviction and
  `active_bytes`. `inspect` and `run --dry-run` plan and report from the same
  settled reading, so a seal between the reads cannot make the plan and its
  figures describe different versions, and a store that never settles makes
  them report `unsettled` with no plan; the report's `active_bytes` comes from
  the final snapshot its row count came from. Five complete-run regressions and
  two scan regressions pin the fixes, and each was checked against a mutation
  that restores the pre-fix behaviour. The report builders moved to
  `outcome_archive_report` and the seal-once test seam to
  `outcome_archive_helpers`, keeping every module under the 400-line ceiling,
  and `settled_snapshot` accepts the absent retention floor the planner can
  read. The saved error-mode gate was reassessed: `StoreFiles.load` is renamed
  `load_and_settle` to state the repair, seal or compaction it may perform, and
  `ArchiveLock` remains at the `run_archive` composition root, exercised
  through the real lock. All seven commit gates passed once each on the final
  tree: `check-fmt`, `lint` (272 reasoned duplication exceptions), `typecheck`,
  `test` (4554 passed, 86 skipped, 76 snapshots), `markdownlint`, `nixie` and
  `audit`.
- [x] Whole-plan revision-20 remediation (2026-11-20): the revision-19
  assessment's two retention findings are fixed. The active scan now always
  reports the committed bytes and rows it measures, so both budget walks count
  the header and the committed rows even without an age cutoff; an uncommitted
  append no longer evicts an archive the committed rows fit beside, pinned by a
  byte-budget regression that was mutated back to the pre-fix behaviour. The
  final report re-plans retention from its own settled reading, so a writer
  append or seal after the last pass reports `unsettled` at exit 75 instead of
  claiming the store is within its bounds, pinned by a settlement regression
  that was mutated likewise. The saved error-mode gate's five items are
  addressed: every report builder reads its finish time through
  `ArchiveContext.clock`, which `RunOptions.clock` supplies; `os.urandom` moved
  behind `ArchiveContext.quarantine_token`; the default configuration path and
  the environment-derived store are resolved when the command runs;
  `read_retention_floor` returns a typed `RetentionFloor` whose `state` names
  `recorded`, `missing`, `unreadable` and `invalid`; and `settled_snapshot`
  receives the typed floor's sequence. New tests pin the injected finish clock
  for an executed, an unsettled and a busy run, the injected quarantine token,
  and both run-time defaults, and each was checked against a mutation that
  restores the old behaviour. The first lint gate after those changes reported
  four `C0302 too-many-lines` violations, so the tree splits them without a
  behaviour change: `outcome_archive_state` derives the retention floor and
  persists the floor and report, while the clock, CLI-default and
  report-settlement tests moved into their own modules, every file now under
  the 400-line rule. The split moved duplication families: the record builder
  the CLI and verify suites each repeated is now
  `outcome_test_helpers.records_at`, the seal fixtures are
  `outcome_archive_helpers.seal_five_rows` and `seal_one_row`, the two
  append-after-retention report tests share `_report_after_an_append`, and the
  composed-trim pair shares `outcome_test_helpers.records_around_a_cutoff`; the
  TYPE_CHECKING-prelude allow entry names the modules the split produced, and
  the composed-trim exception is retired because the shared fixture removed the
  family it excused. All seven commit gates then passed once each on the final
  tree: `check-fmt`, `lint` (269 reasoned duplication exceptions), `typecheck`,
  `test` (4564 passed, 86 skipped, 76 snapshots), `markdownlint` (43 files, 0
  issues), `nixie` and `audit`.

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
- A sealed source's size is exact, but it is not the whole measurement a budget
  needs. Without writer metadata the planner knows the source's bytes and
  nothing about its span, so an entry that age eviction will remove whole still
  inflates the total the byte walk acts on, and a budget that counts it can
  delete an older archive the settled store keeps.
- Narrowing a follow-up plan to the work a run has not done is easy to do in a
  way that inverts eviction order. Filtering failed sequences out of the
  follow-up trims and deletes let a newer trim run after an older deletion had
  failed: the store then discarded rows from the newest archive while the
  oldest entry the budget asked for was still present. The failed-eviction
  barrier, which stops every step at or after the oldest failed sequence, is
  what keeps the follow-up pass as strictly oldest-first as the first plan; a
  regression test that fails only the older deletion pins it.
- Interrogate's repository-wide percentage is rounded to one decimal place, so
  a few missing nested-function docstrings still report 100.0% in the
  `make lint` gate while a focused run over the same files reports 67% or
  88.6%. The gate remains honest for the tree as a whole but cannot prove each
  module, so new nested helpers were also checked with a focused
  `interrogate --fail-under 100` run before the gate.
- A settled store can still inspect as over budget when the active file moves
  after a trim. The first plan counts the active rows, the trim rewrites the
  archive to the remaining allowance, and a writer that appends afterwards
  re-grows the total; suppressing the follow-up's trim because "this run
  already trimmed that archive" reports `ok` while the store is over its cap.
  The suppression has to be keyed on the measurement the trim was planned
  against, not on the archive alone.
- A seal between a follow-up pass's two reads is invisible to either read
  alone: the inventory predates it and the active file has already lost the
  rows, so the plan counts neither copy. Reading the inventory again after the
  active file is what makes the move visible; a seal after the second read is
  consistent with the active snapshot and needs no retry.
- Measuring the active file with two calls is a mixed-version bug even without
  the age rule. A `stat` for the size followed by an open for the rows can
  describe a writer that replaced the path between them as one enormous file
  with another's rows, or as a small file whose rows belong to the larger
  version. Opening once and reading both figures from that descriptor is the
  only pairing that cannot be split by a writer the archiver does not lock out.
  The regression test moves a seven-row file over a two-row path inside the
  open seam, and a fresh `stat` mutation reports the replacement's size beside
  the opened version's rows.
- Writing the three fixes pushed two modules past the 400-line ceiling. The age
  decisions are now `outcome_archive_age`, and the active snapshot and the
  retained-history summary are `outcome_archive_history`; the budget module and
  the planner tests import `ActiveHistory` from the history module, and the
  planner imports the two age helpers. The split is by responsibility rather
  than by line count: the planner now reads as planning, not as age arithmetic
  or snapshot bookkeeping.
- A budget can be deferred by two unrelated reasons that need different
  reporting. A configured record cap is an operational invariant: a run that
  cannot count the unsealed rows fails with `unreadable` at exit 1. An
  age-aware byte budget is a planning decision: the surviving bytes are needed
  only to choose an eviction, so an unmeasured active file defers the walk and
  the run reports `unsettled` at exit 75. `_active_is_measurable` keeps the two
  apart, and `_pending_work` returns an empty deferred plan so `retain` cannot
  mistake "nothing to do" for a settled pass.
- The retained-history check a `stat` performs is not a substitute for a
  second size reading around the scan. A descriptor's size at open time says
  nothing about rows appended while the scan reads, so the only pairing that
  holds is the size before the scan and the size after it; a changed size makes
  the scan a version no other figure describes, and the retry is what lets a
  writer that appends once still be measured in the same pass.
- `inspect` is not a separate reading path. Building a dry run's plan from one
  snapshot and its report from another lets a seal between them show a figure
  no plan was ever made against, so the settled pair is read once and shared.
  `_plan` therefore returns the pair and the caller decides whether to report a
  plan or an `unsettled` result without one.
- A shared syntax shape is a duplication family even across production and
  test modules. The repository-wide `TYPE_CHECKING` prelude is allowed as one
  family whose entry must name every member, so the two modules that gained the
  prelude in this revision (`outcome_archive.py` and
  `outcome_archive_report.py`) were added to that entry; the duplication gate
  then reported 272 allowed reasoned exceptions.
- The settlement suite's hook moved with the plan's reading. It identified the
  final report snapshot by "no cutoff", because only the report read the active
  file without one; giving the final snapshot the policy cutoff, so its budgets
  count the surviving rows, invalidated that marker. The hook now counts
  `settled_snapshot` calls and arms on the second: an executed run calls it
  exactly twice, once to plan and once for the report, so the injected writer
  change lands after the last retention pass however many attempts the report
  needs to settle.
- An oversized retention record is not an I/O fault. The private reader raises
  `OversizedFileError` for a readable file past the bound, and that class is an
  `OSError`, so mapping every `OSError` to `unreadable` would report a file
  this code never writes as a filesystem problem. The specific error is caught
  first and reported as `invalid`.

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
- Each follow-up pass re-plans the whole store and performs the archive,
  reconcile, heal and orphan work it discovers before applying the budget, and
  the run reports `unsettled` (exit 75) when work remains after three passes.
  The writer can seal between the first plan and the first pass, so the plan's
  snapshot is not the store the budgets must describe; discovering the seal
  inside the pass is what lets one firing meet a record cap. `unsettled` is
  deliberately retryable rather than a failure: the store is intact, and the
  next firing continues. The follow-up loop lives in `outcome_archive_retain` so
  `outcome_archive_run` stays a per-segment executor and both modules stay
  under the 400-line limit.
- The follow-up pass never retries an eviction step that failed in this run,
  and it stops every step at or after the oldest such sequence. The first plan
  walks trims and deletions in sequence order, ending at the first failure, so
  a pass that skipped only the failed sequence would let a newer trim or
  deletion discard newer history while the older entry the budget asked for
  remained. The barrier keeps the two walks the same order; the failure is
  already in the report and the next firing retries it.
- The retention walk's ordering and fit are stated as two Hypothesis
  properties over generated measured inventories rather than as more examples.
  The example tests pin each boundary of the byte and record rules, but the
  contract that eviction is an oldest-first whole-entry prefix, that only the
  first survivor is rewritten, and that the estimate fits the bound is a
  statement about every history, not about the sizes a test author thought of.
  Both properties run the pure planner, so they need no filesystem and stay
  fast. Their negative controls replace the byte allowance or the record trim
  with a no-op: each mutation makes its property fail for the intended reason.
- The follow-up pass suppresses a trim only when it matches both the recorded
  trim and the archive measurement it was planned against. A count trim and a
  byte allowance are relative to the document they rewrite, so repeating one
  against the same measurement would discard rows a later pass has not
  accounted for; the same allowance proposed for a re-measured archive is a new
  decision. Keying the suppression on the sequence alone let a store the active
  file re-grew past the cap report `ok` while over its bound.
- A follow-up pass reads the inventory, the active file, then the inventory
  again, and abandons the pass for a fresh pair of reads when the two scans
  disagree. A seal between the two reads moves rows out of the active file and
  into a source the first scan does not list; planning from that pair counts
  neither copy and can leave the store over its bound. A seal after the second
  scan is harmless, because the plan then describes the store as it stood when
  the active file was read. The retry spends one of the three bounded passes
  rather than looping without bound.
- A pass that quarantines an archive or a sealed source stops before eviction.
  Quarantine moves rows out of the retained history, so the totals the plan's
  budget was computed from no longer describe the store, and evicting against
  them could discard history a fresh plan would keep. Only an orphaned sidecar
  leaves the totals true, because it holds no rows, so its quarantine still
  lets the pass evict. The run records the fault, and the next firing plans
  from what remains.
- Settlement is confirmed by a final plan that mutates nothing, not by the
  loop's exit alone. A pass may finish the last operation the loop can see, so
  a run that stopped after its third pass would report `unsettled` even though
  a fresh plan finds no work. The non-mutating check distinguishes "settled"
  from "ran out of passes" without extending the bound.
- The age rule and the record cap's treatment of the active file are stated as
  properties rather than examples.
  `test_age_retention_removes_an_expired_prefix` generates ordered spans and
  asserts that whole deletions are an expired prefix, that every survivor at
  the cutoff is kept, and that every surviving straddler is trimmed at exactly
  the cutoff. `test_a_record_cap_counts_the_unsealed_active_rows` generates
  archive counts and an active-row count and asserts the cap counts both. Both
  were checked with executable negative controls: disabling the age step fails
  the first, and dropping the active rows from the count fails the second.
- The active file's contribution is measured with a cutoff, not from its
  whole-file figures. `active_history` opens the path once and derives the size
  and the rows from that descriptor; when a cutoff is given it also counts the
  bytes and rows at or after the cutoff, and `ActiveHistory.budget_bytes` and
  `budget_records` are what both budget walks consume. The report keeps the
  whole-file figures, because it describes what the store physically holds
  until the writer compacts. The final report, and the plan a dry run reports,
  are read through `outcome_archive_retain.settled_snapshot`, which scans the
  inventory before and after the active read and accepts only a pair of scans
  that agree.
- The three active-history defects are pinned by complete-run regressions in
  `test_outcome_archive_active_budget` (expired active bytes and rows leave an
  eligible archive byte-identical under both bounds, while kept active rows
  still meet the bound and evict it), by a cutoff-scan test and a
  replacement-during-open test in `test_outcome_archive_active`, and by a
  report-retry and a persistent-seal test in `test_outcome_archive_settlement`.
  Each was checked against a mutation that restores the pre-fix behaviour:
  returning whole-file figures from `budget_bytes`/`budget_records` fails both
  expired-row tests, re-statting the path for the size fails the replacement
  test, and replacing the retrying snapshot with a single read fails both
  report tests.
- An append is committed by its newline, so the reader's held-back final row is
  counted only when the stream ended with one. The writer's own rollback
  truncates an interrupted append back out, and counting the row before that
  happens evicted an archive the committed rows fitted beside. `_measure` asks
  `DocumentReader.had_torn_tail` rather than the unsealed readers' broader
  torn-tail rule.
- The scan's size reading is repeated around the row loop instead of the whole
  file being hashed. A descriptor's size is enough to detect an append, and
  re-reading it costs one `fstat`; hashing would make each retry proportional
  to the file. Three attempts cover an append during each scan, and a file that
  never holds still is reported by its size alone so a record cap knows the
  count is unknown.
- A dry run's plan and report share one settled pair. `_plan` returns the
  inventory and active snapshot `settled_snapshot` accepted, and both
  `_inspect` and `_execute_locked` branch on it; a pair that never settles is
  reported as `unsettled` without a plan, never as a plan with figures from a
  different version.
- The run report's `active_bytes` comes from the final snapshot. Its row count
  already did, and pairing the newer count with the plan's older size described
  no version of the file; the executed run's `active` parameter was therefore
  removed from `_execute`.
- Report construction belongs with the report vocabulary. The four findings
  added a dry-run builder and two retryable builders to `outcome_archive`,
  which pushed it past the 400-line ceiling, so `dry_report`,
  `unsettled_report` and `unfinished_report` now live in
  `outcome_archive_report` beside `RunReport`; the orchestrator keeps only the
  decisions about which report to build. The seal-once test seam moved to
  `outcome_archive_helpers` for the same reason, leaving the settlement suite
  focused on its assertions.
- The final report re-plans retention from its settled reading. The writer
  never takes the archive lock, so it can append or seal after the last
  retention pass and before the report measures the store; a report that claimed
  `ok` would tell the timer the budget is met while the next firing still has
  to trim or delete. Only `trims`, `deletes` and a deferred budget make the run
  unsettled: a source sealed just before the read is archival work the next
  firing picks up, and the report already counts its bytes. The re-plan runs on
  the same settled pair the report describes, so it cannot itself report a torn
  store as owing work.
- The retained active history always carries its surviving figures. The
  descriptor size includes an append whose newline the writer has not yet
  written, and a failed write truncates it back out, so a budget that counted
  those bytes would evict closed history for a row that never existed. The scan
  therefore reports the header plus every committed row at or after the cutoff,
  or every committed row when there is no cutoff, and
  `ActiveHistory.budget_bytes`/`budget_records` always consume those figures.
  The whole-file figures remain for the report, which describes what the store
  physically holds until the writer compacts.
- The retention floor is a closed state rather than an optional integer.
  `RetentionFloor.state` names `recorded`, `missing`, `unreadable` and
  `invalid`, so "never recorded" is distinguishable from "recorded and
  damaged", and `_retention_floor` reuses only a `recorded` sequence as its
  baseline. `OversizedFileError` is an `OSError` but a readable file with
  oversized content is `invalid`, not an I/O fault; the private reader's other
  failures are `unreadable`.
- The finish time and the quarantine tag are injected ports. Every report
  builder takes its finish from `ArchiveContext.clock`, which `run_archive`
  fills from `RunOptions.clock`, and `quarantine_file` names its destination
  with `ArchiveContext.quarantine_token`. Tests substitute both to pin the
  exact timestamp and destination rather than tolerating whatever the system
  clock or `os.urandom` supplies.
- The command resolves both of its defaults at run time. `_default_config`
  reads the home in effect when the command runs rather than importing
  `Path.home()` into a module constant, and `main` passes the process
  environment to `_store`, which resolves the store path from it only when
  `--store` is absent. A timer started under another account therefore reads
  that account's configuration and store.

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
   run unless one active segment plus one record transiently exceeds it. A plan
   acts on measured history only: while a surviving entry's size, span or count
   is unknown, it leaves the budget to its follow-up pass. A run reports `ok`
   only when its follow-up passes settle the store as it stands after them; a
   source the writer seals mid-run is archived and measured by a pass, and work
   that still remains reports the retryable `unsettled` status instead. A pass
   that quarantines counted history, or finds the store changed between its two
   reads, does not evict; it leaves the budget to a fresh pass or the next
   firing. Both budgets count only the active rows the age rule keeps, measured
   with the file's size through one opened version, so a row the writer will
   compact away cannot evict closed history and no reading pairs one version's
   bytes with another's rows; a final row without its newline is an uncommitted
   append and is excluded, and an unmeasured active file defers an age-aware
   byte budget instead of evicting against bytes the age rule may have
   released. The plan's, inspection's and report's readings are each retried
   until the inventory and the active file describe one version, and a reading
   the writer never lets settle is reported as `unsettled` with no plan.
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
- Deferral regressions: a complete run keeps an older archive byte-identical
  while a newer unmeasured sealed source is archived, measured and age-evicted
  (`tests/test_outcome_archive_retention.py`), and the planner and CLI tests
  prove `retention_deferred` for a walk that stops at a sealed source it cannot
  trim.
- Writer/archiver interleaving regressions
  (`tests/test_outcome_archive_interleaving.py`) drive the real run and seal a
  source through the archiver's archive seam at a deterministic point. One
  proves a source the writer seals after the plan is archived and then trimmed
  by the same firing, ending at `retained_records == 5` with status `ok` and no
  sealed source left; the other seals on every pass and proves the run reports
  `unsettled` at exit 75 with the last source still present, after which the
  next run settles it to `ok`. Both failed on the pre-fix code for the intended
  reasons, and the failed-eviction barrier is pinned by
  `test_a_failed_older_delete_stops_before_a_newer_trim`.
- Settlement regressions (`tests/test_outcome_archive_settlement.py`) drive a
  real run with one moving part pinned deterministically. One forces the active
  file to grow after an archive rewrite and proves the same allowance is
  decided again, ending at the cap with status `ok`; one seals five rows
  between the inventory and active reads and proves the retried pass archives
  the source and counts it (`retained_records == 5`); one overwrites the newer
  source with a non-document and proves the older archive and its sidecar keep
  their bytes while the run reports `corrupt`; and one seals on each archive
  step up to the fourth source and proves the store the third pass settles
  reports `ok` at exit 0.
- Revision-19 measurement regressions: a paused append whose newline is never
  written leaves the eligible archive byte-identical and is rolled back by the
  writer's own failure path
  (`test_an_uncommitted_append_does_not_evict_an_eligible_archive`); an
  undecodable active file under an age-aware byte budget reports `unsettled` at
  exit 75 with no eviction
  (`test_an_unmeasurable_active_file_defers_an_age_aware_byte_budget`); a scan
  whose size changes across the rows is retried and measured as one version
  (`test_an_append_during_the_scan_is_measured_as_one_version`); inspection and
  a dry run each retry a seal at their active read
  (`test_inspection_retries_a_seal_at_its_active_read`,
  `test_a_dry_run_retries_a_seal_at_its_active_read`); a store that never
  settles reports `unsettled` with no plan
  (`test_inspection_reports_unsettled_when_no_snapshot_holds`); and the report's
  `active_bytes` belongs to the version its row count came from
  (`test_the_report_measures_the_active_version_it_describes`). Each was
  checked against a mutation restoring the pre-fix behaviour: counting the
  held-back row fails the paused-append test, removing the measurability check
  evicts the archive, a single scan fails the append and report tests, and a
  single inventory/active pair fails all three inspection/dry-run tests.
- Planner retention properties in
  `tests/test_outcome_archive_plan_properties.py`: four properties over 50
  generated histories each assert that whole deletions form an oldest-first
  prefix, at most one straddler is trimmed, `retention_deferred` stays false,
  the byte estimate and the record cap both fit the bound (exactly the bound
  whenever a trim is planned), an expired entry is never kept while a fresh one
  is deleted, every surviving straddler is trimmed at the cutoff, and the cap
  counts the active rows. The negative controls make `_byte_trim` return the
  existing trim (the byte property fails on "the planned history fits the
  budget"), make `_record_trim` return `None` (both cap properties fail on "a
  measured history decides its cap"), disable the age step (the age property
  fails on the deleted prefix or the trim set), and drop the active rows from
  the count (the active-row property fails on the cap). Every mutation was
  reverted and the properties pass again.
- A benchmark target reporting compression ratio, elapsed time, child peak RSS
  and peak temporary disk use on generated representative data.

Non-vacuity: every boundary test exhibits at least one record on each side of
the threshold; the memory tests compare two sizes so a constant-buffer failure
or an accumulating buffer both fail, while the materializing read's measured
growth proves the traces observe rows; the segment-count test asserts its
fixture really sealed more than three times the segments before comparing
peaks, so a writer that ignored the segment size could not make it pass
vacuously; and disabling the publication gate makes
`test_a_temporary_that_does_not_match_its_facts_is_not_published` fail. The
interleaving regressions assert the sealed source exists before asserting it
was archived, so a seam that never fired could not make either pass; the
unsettled test asserts the source is still present as well as the status, so a
run that archived everything and mislabelled itself cannot pass, and the
following run's `ok` proves the reported work was real. The planner properties
have executable negative controls: returning the existing trim from
`_byte_trim` fails the byte property and returning `None` from `_record_trim`
fails the cap property, each on the fit assertion, so neither property can pass
by never planning a trim. The generated sizes and caps span empty, exact-fit
and over-budget histories, and each property's surviving-sum branch proves the
no-trim case is reachable. The age property generates ordered spans whose
widths put entries wholly before the cutoff and entries spanning it in the same
history, and disabling the age step makes it fail, so its pass is not vacuous;
the active-row property's generator reaches an excess the active rows alone
cause, and dropping them from the count makes it fail on the cap. The
settlement regressions assert the observable outcome before the status - the
second trim, the archived source, the untouched archive and sidecar, and the
four archives - so a seam that never fired cannot make any of them pass. The
active-history regressions were each checked against a mutation that restores
the defect: returning the whole-file figures from `budget_bytes` and
`budget_records` makes both expired-row tests fail (the archive stops being
byte-identical, and the reported count drops to the post-eviction 5), reading
the size with a fresh `stat` makes the replacement test report 4349 bytes
beside the two rows of the 1319-byte opened version, and replacing the retrying
snapshot with a single read makes both report tests fail (the sealed source is
undercounted, and the persistent seal reports `ok` instead of `unsettled`).
Each mutation was reverted and the focused suite passed again.

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
- Revision 15 records the whole-plan revision-14 assessment's two findings.
  A surviving sealed source with no writer metadata now defers the byte and
  record walks as well: its bytes are exact but its span is not, so the plan
  cannot see the age eviction that will remove it, and a total that counts it
  would delete an older archive the settled store keeps. A walk that stops at
  an entry it cannot measure or rewrite now reports an unsettled budget, so
  `inspect` and `run --dry-run` say the retention is deferred instead of
  presenting the zeroes as final. A complete-run regression keeps an older
  archive byte-identical while the newer unmeasured source is archived,
  measured and age-evicted, and a CLI regression covers a measured sealed
  source that straddles a byte budget, whose archive the real run then trims.
- Revision 16 records the whole-plan revision-15 assessment's finding. The
  writer can seal a source between the plan and the follow-up passes, so each
  pass now re-reads the store and performs the archive, reconcile, heal and
  orphan work it discovers before applying the budget; a record cap is
  therefore met in one firing even when the writer seals mid-run. The passes
  are bounded, and work still pending after them reports the new retryable
  `unsettled` status at exit 75 rather than `ok`. The follow-up loop moved to
  `outcome_archive_retain`, which narrows each pass to work this run has not
  applied and applies the failed-eviction barrier so a newer trim never follows
  an older failed deletion. Two deterministic interleaving regressions pin the
  archive-and-cap outcome and the unsettled status, and the users' guide and
  ADR 0010 describe both. The revision also adds the planner retention
  properties the revision-15 assessment asked for, with negative controls that
  make each property fail when its trim or cap step is disabled.
- Revision 17 records the whole-plan revision-16 assessment's four findings.
  A trim is suppressed only against the archive measurement it was applied to,
  so an allowance the active file re-grew past is decided again instead of
  being skipped. A pass re-reads the inventory after the active file and
  replans when a seal moved rows between the two reads. A pass that quarantines
  an archive or a source stops before eviction, because the budget's totals no
  longer describe the store. A final non-mutating plan distinguishes a settled
  store from a loop that ran out of passes. Four complete-run regressions live
  in the new `test_outcome_archive_settlement`, the planner gained an
  age-retention and an active-row cap property with executable negative
  controls, the `seal_active` fixture and the archive-and-seal seam moved into
  `tests/outcome_archive_helpers.py` so both interleaving suites share them,
  and the duplication gate's prelude and import families are recorded as
  reasoned exceptions.
- Revision 18 records the whole-plan revision-17 assessment's three findings.
  The budgets now count the active rows the age rule keeps, so expired rows
  cannot evict eligible closed history; the archive they would have evicted is
  asserted byte-identical for both bounds. The active file is measured through
  one descriptor, so a writer that replaces the path mid-read cannot pair one
  version's bytes with another's rows. The final report reads the inventory
  before and after the active read and retries until they agree, reporting
  `unsettled` at exit 75 when the writer never lets it settle. The extra
  content pushed two modules past the 400-line ceiling, so the age decisions
  moved to `outcome_archive_age` and the active snapshot and retained-history
  summary to `outcome_archive_history`. The users' guide, the developers' guide
  and ADR 0010 describe the surviving-row measurement, the one-descriptor read
  and the retried report.
- Revision 19 records the whole-plan revision-18 assessment's four findings.
  A final row without its newline is now excluded from every active-file count,
  because it is the writer's append in flight and its failure path truncates it
  back out; an archive the committed rows fit beside stays byte-identical. An
  active file that cannot be decoded defers an age-aware byte budget instead of
  evicting against bytes the age rule may have released, and the run reports
  `unsettled` at exit 75. The active scan reads the size immediately before the
  rows and confirms it immediately after, retrying a version the writer
  appended to; a complete-run regression appends a row in that window and
  checks the inspection's planned eviction, the run's applied eviction and the
  reported `active_bytes`. `inspect` and `run --dry-run` build their plan and
  report from the same settled reading, report `unsettled` with no plan when no
  reading holds, and the run report takes `active_bytes` from its final
  snapshot. `final_snapshot` is renamed `settled_snapshot` because it now
  serves the plan as well as the report. The users' guide, the developers'
  guide and ADR 0010 describe the uncommitted row, the deferred byte budget and
  the shared settled reading, and `run_archive`'s new branches were extracted
  into `_inspect` and `_execute_locked` to keep its lint limits. The 400-line
  ceiling also forced the report builders into `outcome_archive_report`
  (`dry_report`, `unsettled_report` and `unfinished_report`) and the seal-once
  test seam into `outcome_archive_helpers`; `settled_snapshot` now accepts the
  absent retention floor the planner's reading can return. Revision 19 also
  reassesses the saved error-mode gate: `StoreFiles.load` is renamed
  `load_and_settle`, because it repairs, seals or compacts what it reads and
  the old name described only the read; `ArchiveLock` stays constructed at
  `run_archive`, the composition root, because busy and no-headroom are
  exercised against the real lock rather than a substituted one, and an
  injected factory would add a port with no second implementation.
- Revision 20 records the whole-plan revision-19 assessment's two retention
  findings and the saved error-mode gate's remaining items. The active scan
  always reports the committed bytes and rows it measures, so both budget walks
  count the header and committed rows even without an age cutoff and an
  uncommitted append cannot evict an archive the committed rows fit beside. The
  final report re-plans retention from its own settled reading, so a writer
  append or seal after the last pass reports `unsettled` at exit 75 instead of
  claiming the store is within its bounds while the next firing still has to
  evict. Every report builder now takes its finish time from
  `ArchiveContext.clock`, injected from `RunOptions.clock`; `os.urandom` moved
  behind `ArchiveContext.quarantine_token`; the default configuration path and
  the environment-derived store are resolved when the command runs; and
  `read_retention_floor` returns a typed `RetentionFloor` naming `recorded`,
  `missing`, `unreadable` and `invalid` states, whose sequence alone
  `settled_snapshot` and `_retention_floor` consume. New tests pin both
  retention fixes and each injected port, and every one was checked against a
  mutation that restores the old behaviour. The users' guide, the developers'
  guide and ADR 0010 describe the re-planned final report and the floor's named
  states.
