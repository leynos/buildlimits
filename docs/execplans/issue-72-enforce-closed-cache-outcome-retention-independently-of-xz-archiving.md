# Enforce closed cache-outcome retention independently of XZ archiving

This ExecPlan is a living document. The sections `Progress`,
`Surprises & discoveries`, `Decision log` and `Outcomes & retrospective` must
be kept up to date as work proceeds.

Status: IN PROGRESS (2026-11-27; plan recorded, implementation started).

## Purpose / big picture

Issue #72 requires a timer-friendly maintenance path that enforces the
configured cache-outcome retention policy over closed history without first
compressing sealed segments to XZ. The existing `run` command applies the same
validated policy, but it archives every sealed source before its eviction
phase, so a host that only needs retention enforcement cannot avoid the
compression cost, and an age or byte straddler that is still sealed cannot be
trimmed at all.

The change adds a second operation, `retain`, to
`build-limits-outcome-archive`. It shares the validated `RetentionPolicy`, the
archive lock, the settled snapshot, the planner's ordering, oldest-first
eviction, the retention floor and the documented exit codes. It never schedules
archival. A sealed source that straddles a bound is rewritten in place as
verified plain JSON Lines, protected by a durable marker, so an interrupted
maintenance pass is repaired rather than trusted.

## Constraints

- Reuse the validated retention policy and its age, logical-byte and optional
  record-count bounds. Byte and record eviction follow append order; age expiry
  follows observation time, with cutoff equality retained.
- Enforce the bounds across active and closed history (sealed and archived)
  without requiring new sealed history to be compressed first.
- Keep working memory bounded: every pass streams one row and one chunk.
- Recover safely from interruption and insufficient disk space, and report
  limits, eviction reasons and maintenance failures as bounded diagnostics.
- Archive-mode planning and execution stay bit-for-bit unchanged.

## Progress

- [x] Load repository guidance, retention, archive planning, eviction, segment
      scan, file, state and report modules.
- [x] Rename the branch and open the Lody session record on issue #72.
- [x] EP-1: sealed marker, inventory distrust, in-place trim and repair.
- [x] EP-2: retain mode in the planner, executor, aggregation and settlement.
- [x] EP-3: clock guard, operation-aware state, report and metrics.
- [x] EP-4: `retain` subcommand, operation-aware rendering, users' guide
      procedure with tested service/timer examples, ADR 010 addendum and the
      repository-layout entry.
- [x] EP-5: no-XZ enforcement, accounting, restart, clock, fault, memory and
      documentation-example tests.
- [x] EP-6 (review round 1): CodeRabbit's 17 findings were all valid and are
      fixed. The clock decision is re-evaluated under the lock, a failed
      baseline write preserves the report's state flags and is persisted, an
      unrepresentable baseline number is refused, `narrow` distinguishes proven
      absence from an inspection fault with `is_proven_absent`, a completed
      removal is not an eviction barrier, strict cleanup propagates inspection
      errors, reports say whether a baseline was written, a preview detects a
      jump without claiming withholding, and the guide's output examples are
      marked and exercised.
- [x] EP-6 (review round 2): the five follow-up findings are fixed. The
      guide distinguishes a failed first baseline write from a failed update,
      and marks the preview command rather than the mutating one; the clock
      test configures both bounds so the byte bound is asserted while the age
      bound is withheld; the pending test leaves its path absent so a faulty
      `Path.exists` check would fail it; the single-file duplication exception
      keeps the shape the installed gate emits and accepts.
- [x] EP-6 (review round 3): the byte allowance now excludes a final
      undecodable fragment the reader drops, with a regression test that fails
      without the adjustment; the guide distinguishes a failed first baseline
      write from a failed update.
- [x] EP-6 (review round 4): the guide states that one applicable bound can
      require whole-source removal, and limits the no-compression claim to
      sealed history.
- [ ] EP-6 (remaining): fifth review, ready for review, merge.

Gate evidence at this point: `make check-fmt`, `make lint-python`,
`make typecheck`, `make lint` (including dead code and duplication) and
`make test` (5,039 passed, 89 skipped) all succeed.

## Surprises & discoveries

- `outcome_archive_retain.retain` is the post-plan retention aggregation, not a
  command. The new `retain` operation shares its bounded follow-up passes.
- Sealed metadata is written by the writer with `schema`, `sequence`,
  `salt_id`, `record_count`, both observation bounds and `sealed_at`; a rewrite
  must preserve the seal time.
- `outcome_store_sealing.highest_segment_name` parses any name beginning with
  `segment-` and yields the same sequence for a marker, so markers cannot push
  the writer's counter; maintenance temporaries start with `.` and are ignored.

## Decision log

- Record decision: add a `retain` operation to the existing command rather than
  a second executable, so the policy, lock, floor and exit codes cannot drift.
- Record decision: trim a sealed straddler in place with a durable
  `segment-<sequence>.jsonl.trimming` marker and a `retain-` temporary prefix;
  the inventory distrusts a marked source's metadata, and repair re-derives it
  from the file.
- Record decision: keep existing archives as archives (recompress or delete
  whole); never convert an archive back to plain.
- Record decision: a persisted `clock-baseline.json` (boot identifier, wall
  time, boot time) withholds age eviction for one locked run after a jump of
  more than one day; capacity bounds still apply. `inspect` and `--dry-run`
  report the decision without applying it or writing a baseline.
- Record decision: the module grew past the 400-line bound, so planning and
  execution under the lock (`outcome_archive_execute`), the clock decision and
  baseline (`outcome_archive_firing`), pending-work bookkeeping
  (`outcome_archive_pending`), duplicate reconciliation
  (`outcome_archive_reconcile`), retention decisions
  (`outcome_archive_retention_plan`) and the plain renderer
  (`outcome_archive_render`) each have their own module. `outcome_archive`
  keeps the entry points and fault mapping.
- Record decision: `TrimWalk` in `outcome_archive_trim` owns the row-decision
  counters, so the archive writer and the plain writer cannot disagree about
  which rows a policy drops.

## Verification plan

- `tests/test_outcome_retention_sealed.py`: no-XZ enforcement for age, byte and
  record bounds, partial and whole eviction, mixed active and closed
  accounting, existing archives, restart, reduced bounds and clock movement.
- `tests/test_outcome_sealed_trim_faults.py`: `ENOSPC`, verification failure,
  interruption after each publication step, failed deletion barriers.
- Extended CLI, report-line and metrics tests for `retain`, `operation`,
  `clock_skew`, exit codes, `last-retain.json` and the `retention_run` event.
- Memory scaling across 4,000 and 16,000 rows using the existing trace
  thresholds.
- `make check-fmt`, `make lint`, `make typecheck`, `make test`,
  `make markdownlint`, `make nixie`, `make all`.

## Outcomes & retrospective

Pending.
