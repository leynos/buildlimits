# Architectural decision record (ADR) 010: archived cache-outcome history

## Status

Proposed. The decision is implemented on the issue #63 branch; accept this
record when that work merges. It refines the outcome store in
[ADR 005](adr-0005-cache-outcome-recording.md) and keeps the evidence boundary
in [ADR 007](adr-0007-cache-outcome-observation-port.md).

## Date

2026-11-19.

## Context and problem statement

The cache-outcome recorder writes one JSON object per line to
`$XDG_STATE_HOME/buildlimits/cache-outcomes.jsonl`, defaulting to
`~/.local/state/buildlimits/`. ADR 005 D17 bounded that document at 12 MiB and
10,000 rows, compacting it in place in the writer thread.

Issue #63 asks for 30 days of retained history. At the measured load, about 6.8
outcome rows per second at roughly 661 bytes per row, that is about 11.7 GB of
uncompressed JSON Lines. No single JSON Lines document of that size can be
compacted in place: every compaction rewrites the whole document, the writer's
append path carries the cost, and the hard 16 MiB append bound would discard
rows long before the requested span was reached.

Two further forces shape the answer. The supervisor must not compress 11.7 GB
on its event loop, because a multi-second XZ pass would delay admission and
token accounting. And the active file cannot have a second writer: an
out-of-process archiver that rewrote or truncated it would race the writer
thread, whichever lock either side took.

Reading has the matching gap. Replay and dataset export read only the active
document, so any history moved out of it would become invisible to them, and a
salt change already partitions history into domains that must not be compared
across the boundary.

The work therefore needs a retention policy that can bound a large history, a
way to move closed history out of the active file without a second writer, and
a reader that spans whatever states the history is in. Permanent timer
provisioning belongs to
[dev-env-rocky #424](https://github.com/leynos/dev-env-rocky/issues/424); this
record covers the command and the storage contract it acts on.

## Decision drivers

- Keep one writer per file. The supervisor alone owns the active document, and
  the archiver alone owns closed history, without a shared lock.
- Bound retained history by logical uncompressed size, so the budget describes
  how much history is kept rather than how well it happens to compress.
- Keep archive cost off the supervisor's event loop and out of its memory
  budget.
- Make every published archive provable against its source, and leave no crash
  state that a later run cannot settle.
- Give a systemd timer a small, closed result vocabulary: statuses, reasons and
  exit codes, with no host-specific detail in the journal.
- Preserve the record schema, the salt identifier and the existing behaviour of
  deployments that do not opt in.
- Add no runtime dependency.

## Requirements

### Functional requirements

- A retention policy bounds history by age and by logical bytes, with an
  optional explicit record cap, and evicts the oldest closed history first.
- A run archives each sealed source, verifies the archive, publishes it, and
  removes the source only afterwards.
- A run applies the age and byte bounds to archived history, including trims
  that cut a partially expired segment.
- Replay and dataset export can read archived, sealed and active history in
  append order, for the newest salt domain, a named domain or every domain.
- `inspect` and `run --dry-run` report the plan without changing a file.

### Technical requirements

- Only the writer thread appends to, truncates, replaces or renames the active
  file.
- Compression and verification stream in fixed-size chunks and never buffer a
  segment's rows. The inventory, plan and report hold one small record per
  segment, so peak memory is the encoder's working set plus a bounded
  per-segment allocation, measured at about 4 KiB; it must not follow the
  retained history's size.
- XZ comes from the standard-library `lzma` module with an explicit preset and
  CRC64 check.
- Publication uses a private temporary file, an `fsync`, a no-clobber link and
  a sidecar written after the archive.
- Every failure maps to a closed reason and an exit status a timer can act on.
- Every path the archiver touches is private to its owner and is validated
  against symlinks.

## Options considered

### Option A: raise the bounds on one compacted document

Keeping one JSON Lines document needs a compaction pass that rewrites the whole
file. At 11.7 GB that pass cannot run in the writer thread without stalling
appends, and it cannot run in another process without a second writer. The 16
MiB append guard would also have to grow, which weakens the corrupt-store
detection ADR 005 relies on. Rejected.

### Option B: an archiver that rewrites the active file

An out-of-process archiver could compress and truncate the active document,
either by copy-truncate or under a lock shared with the writer. Both put two
processes on one file: copy-truncate loses appends that land during the copy,
and a shared lock makes admission wait on XZ compression. The approach also
verifies nothing, so a bad archive would be discovered only when the source was
already gone. Rejected.

### Option C: writer-sealed segments, verified XZ archives, private archiver lock

The writer seals the active document at a fixed high-water mark by atomic
rename. A separate archiver compresses each sealed source to XZ, verifies the
archive, publishes it without clobbering, then removes the source. It holds a
private lock of its own, never writes the active file, and reads it once,
bounded, to report the retained history. Chosen.

Table 1 compares the options on the properties the decision drivers name.

| Property                   | A: one document | B: rewrite active | C: sealed and archived |
| -------------------------- | --------------- | ----------------- | ---------------------- |
| Writers of the active file | One             | Two               | One                    |
| Compaction cost            | Writer thread   | Writer waits      | Separate timer process |
| Peak memory                | Whole document  | Whole document    | One chunk and one row  |
| Archive integrity checked  | Not applicable  | No                | Yes, before publishing |
| Runtime dependency added   | None            | Compression tool  | None                   |

_Table 1: Comparison of the retention storage options._

## Decision outcome / proposed direction

### D1: retention is bounded by age and by logical uncompressed bytes

One policy object, `RetentionPolicy`, answers every retention question for the
writer, the archiver and the read-only history view, so the three cannot
disagree about a bound.

- Age is `cache_outcomes_max_age_days`, 30 days by default; zero disables age
  eviction. A record whose observation time equals the cutoff is retained, and
  one older than the cutoff is not.
- The byte budget is `cache_outcomes_max_bytes`. It counts logical uncompressed
  bytes across active, sealed and archived history: a sealed file by its
  `stat`, an archive by its sidecar's `logical_bytes`. The header row and each
  row's terminating newline are part of that total. The active file contributes
  only the bytes of the rows the age rule keeps, measured in the same scan that
  counts them: a row the writer will compact away is not retained history, and
  counting it would evict closed history to make room for bytes retention has
  already released. When the active file cannot be decoded, the committed bytes
  are unknown, so the byte walk is deferred rather than decided from the whole
  file, whether or not an age cutoff applies: the whole file includes any
  append the writer has not committed and may roll back, so evicting against it
  could discard an archive the settled store keeps. Zero disables the byte
  bound, and is reported as a disabled bound rather than a zero-byte target.
- The record cap is `cache_outcomes_max_records`. A positive value applies in
  either mode. When it is omitted, a segmented store has no count cap and a
  legacy store keeps the historical 10,000-record cap. The cap bounds the whole
  store, so the archiver counts the active rows the age rule keeps and each
  sealed source's recorded count as well as the surviving archives.
  Oldest-first eviction considers archives and sealed sources together: a
  sealed source outside the budget is deleted whole rather than compressed into
  an archive the same run would remove, while the writer's active file stays
  outside the archiver's reach, and a sealed source that straddles the age or
  byte cutoff is archived untrimmed and trimmed by the run's follow-up plan.
  The walks act only on measured history: while a surviving sealed source has
  no writer metadata, or an archive has no trustworthy sidecar, the plan cannot
  tell which rows will survive and leaves the whole budget to its follow-up
  pass rather than choose a deletion from a partial total. A source the writer
  seals during the run is discovered by that follow-up pass and archived before
  the budget is planned over it, so a cap can still be met in one firing. A
  pass does not repeat a trim against the archive version it already rewrote,
  but the same allowance proposed for a re-measured archive is a new decision,
  because the active file can re-grow the store past the cap after a rewrite. A
  pass that quarantines an archive or a source also stops before eviction: the
  quarantined rows were part of the total the budget was planned over, so
  eviction waits for a fresh plan. A seal that lands between a pass's inventory
  and active-file reads is detected by reading the inventory again, and the
  pass is replanned rather than counting neither copy. The final report is
  bounded the same way: the inventory is scanned on both sides of the
  active-file read and the reading is retried until the two scans agree, so a
  seal cannot leave either copy out of the reported count and span. `inspect`
  and `run --dry-run` plan and report from that same settled reading, so a plan
  and its figures describe one version of the store, and its `active_bytes`
  comes from the reading its row count came from. If new seals keep the bounded
  follow-up passes, or the plan's or report's readings, from settling the
  store, the run reports `unsettled` and exits 75 rather than claiming success;
  the next firing retries.
- The active file is measured through one opened descriptor, so its size and
  its rows describe the same version even when the writer replaces the path
  with a compacted or sealed file mid-scan. The size is read immediately before
  the scan and confirmed immediately after it, and a scan whose version changed
  is retried, so an append during the scan cannot pair the longer file's rows
  with the earlier size. An append is committed by its newline, so a final row
  without one is the writer's append in flight, which a failed write truncates
  back out; it is excluded from the rows a budget counts. A path that is not a
  regular file is refused rather than counted, because following a link would
  let a file outside the store decide retention.
- Segmentation follows the byte budget: a budget larger than one 12 MiB
  segment, or a budget of zero, makes the writer seal closed segments. The
  default budget remains the legacy 12 MiB, so a deployment that writes none of
  the retention keys keeps its current shape.
- Headroom is `cache_outcomes_archive_headroom_bytes`, two segments by default
  and at least one segment when configured. It is temporary disk space checked
  with `statvfs`; it is never part of the retained budget and never appears as
  retained bytes. The check reads free space once, before the pass, so it
  reserves nothing: a write that meets `ENOSPC` afterwards reports
  `no_headroom` per sequence and `EX_TEMPFAIL` for the run.

The 15,000,000,000-byte history requested for this work is a configured policy
value, not a built-in default. Below both bounds and without an explicit cap,
history is retained with no hidden count limit. Physical XZ bytes are reported
per action so an operator can see the compression actually achieved, but they
do not move the retention boundary.

### D2: the writer owns the active file and the archiver owns closed history

Ownership follows file state, not a shared lock.

- The writer is the only process that appends to, truncates, replaces or
  renames the active file. In segmented mode it seals when the next append
  would take the document past `segment_bytes` (12 MiB), and at startup and
  shutdown when the settled file is larger than that bound. Sealing writes the
  next sequence counter and the segment metadata durably, then renames the
  active file into the sealed directory and installs a fresh header-only active
  file. An empty store installs a header-only active file instead of claiming a
  sequence, so the sequence series has no artificial first gap. The age bound
  is applied on ordinary appends as well: once the oldest retained row is older
  than the cutoff, the writer compacts the active file, so a quiet store cannot
  keep expired rows until the next restart. Compaction drops every row observed
  before the cutoff, which makes the check self-limiting: it costs a clock read
  per append and a rewrite only when the bound is crossed.
- The archiver never appends to the active file and never takes the writer's
  queue. It reads the file's size with `lstat` to include those bytes in the
  retained total, and streams its rows once per plan to count them against the
  record cap. Because the writer may be mid-append, an unterminated final row
  is dropped exactly as a torn tail is dropped from a sealed source; a file
  that cannot be read at all contributes its size alone to the diagnostic
  totals, a configured byte budget is deferred until the next firing can read
  its committed bytes, and that firing counts its rows again. The report's
  `retained` document is built from the same settled reading as the plan, so
  its rows, bytes and span describe the version the run measured; a file that
  cannot be decoded leaves its rows and span unmeasured rather than empty.
- Every archiver mutation runs under a non-blocking exclusive `flock` on
  `cache-outcomes.archive/archive.lock`. A second invocation reports `busy` and
  exits 75 instead of waiting. The writer never takes that lock, so there is no
  shared lock and no lock ordering to get wrong. Because the writer can seal
  between the plan and its follow-up passes, each bounded pass re-reads the
  store and archives what it finds before applying the budget. Work that still
  remains after the last pass is reported as `unsettled`, also retryable,
  rather than treating the plan-time snapshot as the whole store; settlement is
  confirmed by a final plan that mutates nothing, so a store the last pass
  finished is reported `ok`.

A sealed source is immutable until its verified archive is published; after
that the archiver owns the segment's lifecycle. Table 2 names the three states
and how each is measured.

| State    | Path                                          | Mutated by | Logical bytes from |
| -------- | --------------------------------------------- | ---------- | ------------------ |
| Active   | `cache-outcomes.jsonl`                        | Writer     | `stat` size        |
| Sealed   | `cache-outcomes.sealed/segment-<n>.jsonl`     | Archiver   | `stat` size        |
| Archived | `cache-outcomes.archive/segment-<n>.jsonl.xz` | Archiver   | Sidecar            |

_Table 2: The retained history states and how each is measured._

Sequence names are zero padded to twelve digits so a directory listing sorts in
append order. The archive directory also holds the sidecars, `archive.lock`,
`retention.json`, `last-run.json`, `quarantine/` and, while a rewrite is in
flight, a `segment-<n>.jsonl.xz.replacing` marker.

### D3: XZ comes from the standard library and every archive is verified

Compression uses `lzma.LZMACompressor` with `FORMAT_XZ`, `CHECK_CRC64` and the
configured preset, `cache_outcomes_archive_preset`. The preset defaults to 6
and is validated in the range 0 to 9. No runtime dependency is added: `lzma`
ships with CPython and the package's dependency set is unchanged.

Reading and writing stream in `CHUNK_BYTES` (64 KiB) chunks with at most one
row buffered, so the encoder and decoder peaks follow the chunk size and preset
rather than the segment or the retained history. The surrounding bookkeeping
does not: the scan holds one entry per segment, the plan one action per entry
and the report one record per performed action, which is O(segments) and
measured at about 4 KiB per segment. The bound is therefore the preset's
working set plus that share, never the history's size.

Before any filesystem publication, the temporary archive is decompressed to its
declared end and decoded as a document. Verification requires:

- the XZ stream to reach its end, with only NUL padding whose length is a
  multiple of four bytes after it, so a truncated file and trailing garbage are
  both faults;
- every row to decode as a supported header or a valid record, within the
  record byte bound;
- the decompressed content's record count, logical byte count and SHA-256 to
  equal the facts accumulated while compressing the source.

A mismatch discards the temporary file and raises a closed fault; nothing is
published. The digest is over the uncompressed content, which is what the
sidecar records and what reconciliation compares on a later run.

### D4: publication is ordered so a crash leaves a readable state

For a new archive the order is:

1. Create a private temporary file in the archive directory with `O_EXCL` and
   mode 0600, write the XZ stream through it and `fsync` the file.
2. Verify the temporary as D3 describes.
3. Publish it with a no-clobber hard link to `segment-<n>.jsonl.xz`, then
   `fsync` the directory. An existing archive is never replaced.
4. Write the sidecar by atomic JSON replacement, so it is either absent or
   complete.
5. Remove the sealed source and the writer's segment metadata, then `fsync` the
   sealed directory.

The uncompressed source is therefore present until a verified archive and its
sidecar are durable. This is the invariant the retention planner and every
recovery path rely on.

A trim rewrites an archive by the same sequence and replaces it only by an
atomic rename of a verified temporary file; a trim that keeps no records
deletes the archive instead. The rename publishes bytes before the sidecar that
describes them, so a process that stops between the two leaves a sidecar
describing the previous archive. Compressed size cannot separate the pair,
because two XZ streams of different content can occupy exactly the same number
of bytes, so the rewrite records its intent first: it writes a durable
`segment-<n>.jsonl.xz.replacing` marker before the rename, and removes it only
once a sidecar describes the archive that is actually published. A scan that
finds the marker refuses that sidecar and routes the archive to the repair
path, which re-derives the facts from the archive itself; the same refusal
covers a sidecar whose recorded physical size does not match the file beside
it. Repair writes the sidecar and then clears the marker, so the marker's
lifetime is exactly the window in which the pair may disagree. Reconciliation
on each run settles the states a crash can leave: a matching archive removes
the redundant source, an unreadable or mismatched archive is quarantined and
the source archived again, and a corrupt source is quarantined while the
existing archive is kept. Quarantine moves a file into
`cache-outcomes.archive/quarantine/` under a name carrying the closed reason
and a random token, so no quarantined file is overwritten. Stale temporary
files are removed at the start of a run, under the lock.

### D5: the timer-facing command has bounded output and closed exit statuses

`build-limits-outcome-archive` has two subcommands. `run` applies the policy,
and `--dry-run` plans without taking the lock or changing a file. `inspect`
reports the same plan and is a dry run by definition. Both accept `--store`,
`--config` and `--json`; the default configuration file is
`~/.config/buildlimits/supervisor.toml`, as it is for the supervisor, and the
default store path comes from the XDG state directory.

The plain output is a bounded set of status lines: status and reason, retained
logical bytes against the budget, active bytes, the age cutoff, the plan's
counts, one line per action name, then only the non-empty integrity lines for
gaps, missing, corrupt and orphan sidecars, and per-sequence failures. The JSON
form is the same report with sorted keys, and carries `gaps_truncated` so a
consumer can tell a complete gap list from a pruned one. The gap list stops at
4096 sequences and then says that more are missing, so even the integrity lines
cannot grow with the number of lost segments. Neither form names a filesystem
path or carries raw exception text; sequences and closed reasons identify what
needs attention.

Exit statuses are part of the contract, as Table 3 records.

| Status | Meaning                                                    |
| ------ | ---------------------------------------------------------- |
| 0      | Success                                                    |
| 1      | Operational failure                                        |
| 2      | Usage or configuration error                               |
| 3      | Corruption, or a segment that could not be processed       |
| 75     | `EX_TEMPFAIL`: the lock is held, or the filesystem is full |

_Table 3: Exit statuses of `build-limits-outcome-archive`._

Status 3 is the report's `corrupt` status, which also covers an unreadable
segment, so a timer can distinguish a retryable condition (75) from one that
needs an operator.

There is one exception to the no-path rule. A configuration file that cannot be
read is reported to standard error before any report exists, and that message
names the file. The report itself never does.

### D6: retention and diagnostic state live beside the archives

Each completed run writes two documents into the archive directory:

- `retention.json` records the schema, the lowest intentionally retained
  sequence (the retention floor), retained logical bytes, the budget, the
  cutoff, the update time, and the `limits` and `retained` documents.
- `last-run.json` is the run's report document, carrying the fields `--json`
  prints, including `limits`, `retained`, `evictions`, `maintenance` and the
  per-action reasons.

Both are written by atomic replacement. The retention floor is why evicting old
history does not look like corruption: the inventory reports a missing sequence
as a gap only at or above the floor. A record that is missing, unreadable or
invalid is reported as that state rather than as a floor. A store that never
recorded one is fresh, so its baseline comes from the history it has. A damaged
record is different: the store can no longer prove which absent sequences
retention removed, so the baseline stays at the lowest sequence any store can
start at. Every absent sequence below the first survivor therefore keeps being
reported, rather than the run deriving a higher baseline from the survivors and
silently raising the floor over a gap.

After archiving and reconciliation, a run replans over the settled store and
applies the trims and deletions the first plan could not see, skipping
sequences it already deleted, quarantined or failed to process. One successful
run therefore leaves the byte budget true, rather than leaving the excess for
the next timer firing.

### D7: history is read in append order and partitioned by salt

`outcome_history.read_history` walks the inventory's effective entries -
archived and sealed segments in ascending sequence, a sealed source taking
precedence over an archive with the same sequence - and then the active file.
Each document is streamed and verified in turn. A corrupt segment becomes a
closed `HistoryFaultError` reason rather than a silently short history.

A salt change partitions history, so a read selects a domain:

- the default is the newest domain, taken from the last document's header,
  which is the active file when one exists;
- `--salt-id ID` reads that domain and reports `unknown_salt` when no retained
  segment carries it;
- `--all-salts` reads every domain in append order without implying that their
  records are comparable.

The replay and dataset commands share these options through
`outcome_history_args`. `--salt-id` and `--all-salts` imply history mode, and
with no flag at all each command keeps reading only the active store, which is
the behaviour that existed before archiving. The snapshot is pinned before
either pass: `outcome_history_pin.pin_history` fingerprints each effective
segment and freezes the active file's bytes, and
`outcome_history_stream.open_history` yields the selected rows one segment at a
time, skipping a domain the selection excludes. The frozen active bytes follow
the store's committed-state rule: a final row without its newline is the
writer's append in flight, which `read_store` never returns and the writer
removes before its next append, so the stream holds it back; a sealed segment
keeps a decodable final row, because a closed document's complete row must not
be lost. A pass that finds a pinned segment replaced fails closed as
`unsettled` before yielding anything; the replay command then takes a fresh
pin, and H2's two passes always compare the same rows. Replay therefore holds
one segment's decoder state rather than the whole retained history. The dataset
snapshot still materializes the stream, because a snapshot holds every selected
record by definition.

### D8: the record schema and salt identifier survive archiving

The archiver writes each decoded row's original bytes and a newline into the XZ
stream, and writes the header row unchanged. An archive therefore decompresses
to the segment's JSON Lines content byte for byte, with one normalization: a
source that does not end with a newline is repaired, so a decodable final row
is kept and terminated while an undecodable one is dropped. Either case is
recorded as `had_torn_tail`. The format version and `salt_id` in the header are
preserved because the header is preserved. The sidecar repeats the salt
identifier so an inventory can partition history without decompressing an
archive.

Compression is an encoding change, never a schema change. No record is
re-encoded, reordered or re-timestamped.

### D9: diagnostics separate the effective policy from retained history

One report shape answers two questions an operator must keep apart, so
`outcome_retention_report` derives both from validated values rather than from
either component's private counters.

- `limits` names the effective policy: the age in days and seconds, the
  logical byte budget, the record cap (`null` when unbounded), the headroom,
  the segment size, whether the store is segmented, and whether age or byte
  eviction is active. A zero age or byte bound appears as zero with its
  eviction flag false.
- `retained` names what the store holds now: total rows and logical bytes,
  the split across active, sealed and archived history, the archives' physical
  bytes, the closed-segment count, the observation span and a `complete` flag.
  The flag is false when a segment's metadata is missing, the active file could
  not be read, or the writer replaced the active file while the run or a dry
  run's planning reads observed it, so a lower bound is never presented as
  exact.
- `evictions` counts trims and deletions under `age`, `bytes`, `records` and
  every combination of those bounds, and each action carries the same reason. A
  bound that is configured but did not cause an eviction is never named.
  `maintenance` reports whether the state documents were installed, so a failed
  write is a retryable `failed` status rather than silent success. A dry run, a
  deferred run and a run that could not start install no state and report
  `state_written` false.
- The supervisor's status document carries the same `limits` derivation and
  the writer's active snapshot, which the renderer prints as one line. The
  worker publishes each state's row count and byte size together, so a status
  read never pairs one state's rows with another state's size. A snapshot the
  writer cannot supply omits the section rather than failing the status read.
  Closed history is reported by the archive command, which owns it.

None of these figures is a cumulative recorder counter or a journal-reader
query cap: `record_count` counts rows retained on disk, not outcomes seen since
start-up, and `--max-records` limits a query rather than the store.

## Goals and non-goals

### Goals

- Retain about 30 days of cache-outcome history under an explicit age and byte
  policy, without a hidden record cap.
- Move closed history out of the active document without a second writer of
  that document.
- Prove every archive against its source before the source is released.
- Keep replay and dataset export working across archived, sealed and active
  history and across salt changes.
- Give a systemd timer bounded output and actionable exit statuses.

### Non-goals

- Change admission, token accounting, recovery or the recorder's observation
  rules.
- Add a runtime dependency or a second compression format.
- Provision the systemd timer in this repository; that belongs to
  dev-env-rocky #424.
- Compare records across salt domains, or migrate history to a new salt.
- Make compression recover outcome labels that sparse observation did not
  record.

## Consequences

### Positive

- Retained history is bounded by one policy the writer, archiver and reader
  share, so a large span can be configured without a silent count cap.
- The supervisor never compresses, so archival cost cannot delay admission.
- The writer and archiver cannot race: no shared lock, no copy-truncate and no
  second writer on the active file.
- Every published archive is verified and every crash state has a
  reconciliation path.
- Replay, dataset export and diagnostics keep working across segment states,
  and no new dependency is introduced.

### Negative

- A timer becomes part of the operation. Until it runs, sealed sources
  accumulate, and the writer reports `archive_backlog` at each eight-entry step
  of the sealed directory.
- Backups and cleanup must now include the sealed and archive directories and
  their state files. Deleting only `cache-outcomes.jsonl` no longer resets the
  retained history.
- The archiver applies the effective policy from the same configuration, so
  running it with the legacy 12 MiB default evicts archived history down to
  that budget. The retention keys must be configured before the timer is
  enabled.
- A crash can still leave a duplicate source or a stray temporary file. The
  next run reconciles or removes it, so the cost is deferred, not lost.
- An archive's usefulness depends on the XZ encoder in `lzma`; a format change
  would need a new sidecar schema rather than reuse of the existing one.

## Known risks and limitations

- Bounded memory depends on the streaming design. A high preset raises the
  encoder's own working set, so the preset trades memory and CPU against size.
  The plan and report also keep one small record per segment; with a very small
  `cache_outcomes_segment_bytes`, that bookkeeping grows with the segment count
  even though no segment's rows are ever buffered.
- Verification proves the archive matches what was written, not that the source
  rows were meaningful; a malformed row is caught only by the record codec.
- A time-based trim cannot be planned in bytes without reading rows, so the
  planner treats it as freeing nothing and the actual trim is applied under the
  lock.
- The report's physical byte figures are per action, not a disk-usage total;
  sidecars, state files and quarantine also occupy the filesystem.
- The archiver reads the active file through the bounded no-follow reader to
  count the rows and span the writer currently holds, retrying a scan the
  writer appended to; it never writes or replaces the file. A retained total
  can still be stale by whatever the writer appended after that read.

## Architectural rationale

The decision keeps one writer per file and one responsibility per process. The
retention policy, the planner, the verification pass and the report are pure or
streaming functions over values, so they are testable without a supervisor, a
timer or a large file. `outcome_archive_fs`, `outcome_archive_encode`,
`outcome_archive_quarantine`, `outcome_archive_files`, `outcome_archive_run`,
`outcome_archive_active`, `outcome_archive_lock` and `outcome_archive_cli` are
the modules that touch the filesystem and the process boundary. That mirrors
the repository's hexagonal boundary: an out-of-process maintenance task
reorganizes files the supervisor produces, and never participates in admission.
The same boundary is why the archive decision could be taken without changing
ADR 005's recording rules.

## References

- [Issue #63](https://github.com/leynos/buildlimits/issues/63): "Archive
  cache outcomes as XZ through a timer-friendly CLI".
- [Issue #64](https://github.com/leynos/buildlimits/issues/64), the
  retention-policy contract this decision implements.
- [dev-env-rocky issue #424](https://github.com/leynos/dev-env-rocky/issues/424),
  permanent host provisioning for the archive timer.
- [ADR 005](adr-0005-cache-outcome-recording.md), the local outcome store, its
  schema and its privacy boundary.
- [ADR 007](adr-0007-cache-outcome-observation-port.md), the cache-outcome
  evidence boundary.
- [ExecPlan for issue #63](../execplans/issue-63-archive-cache-outcomes-as-xz-through-a-timer-friendly-cli.md)
  records the implementation phases and verification plan.
