# Migrating to cache-outcome recording

This guide is for operators moving a host from a buildlimits release that
cannot record cache outcomes to one that provides the feature. Recording is
opt-in, observes cached compilations without changing admission, and writes a
private local store. The upgrade order below keeps every journal reader ahead
of the vocabulary the supervisor can emit.

The design and its thresholds are recorded in
[ADR 005](adrs/adr-0005-cache-outcome-recording.md); the operator-facing
behaviour is described under
[Record cache outcomes](users-guide.md#record-cache-outcomes).

## What changes

| Area              | Before this feature     | With cache-outcome recording                       |
| ----------------- | ----------------------- | -------------------------------------------------- |
| Configuration     | No `cache_outcomes` key | `off` (default) or `record`                        |
| Journal           | No cache-outcome events | `cache_outcome` events and reading reasons         |
| Status            | No outcome section      | A `cache outcomes` line from `build-limits-status` |
| Local store       | None                    | `$XDG_STATE_HOME/buildlimits/cache-outcomes.jsonl` |
| Evidence commands | None                    | `build-limits-cache-replay h2` and `h3`            |

_Table 1: Behaviour added by cache-outcome recording._

Recording also requires `cache_mode = "sccache"`. The supervisor validates that
combination, and the `cache_outcomes` value itself, at startup, so an
incompatible configuration fails before any compiler is admitted. The sccache
version is not checked at startup: an unsupported version is detected on a
later statistics reading, which disables recording while admission continues.

The store format is version 5 and the snapshot format is version 5. An outcome
row adds the UTC `admitted_at` moment captured at admission, `outcome_reason`
and `outcome_attribution` (version 2), the `label_time_provenance` marker that
says whether its label time is proven (version 3), the `generation_digest` that
names the sccache generation its reading observed (version 4), and the UTC
`completed_at` moment with the bounded `attempt` and `supervisor_generation`
correlation tokens (version 5). A snapshot adds the bounded label-loss
diagnostics, including the per-generation breakdown, and, per event, the
`label_observed_verified` marker that carries the same statement as the row's
provenance, plus the completion moment and correlation pair. Version 1 to 4
stores and snapshots remain readable, and their rows report the newer evidence
as `unrecorded` or absent, so a history written before the markers existed
contributes no proven labels, admission times, generation counts or
correlations to the prediction benchmark.

The compatibility promise covers documents a **release** wrote. Snapshots taken
by intermediate revisions of this branch before the omission fields existed
carry an observer block with fewer keys, and the loader refuses it rather than
reading the absent keys as measured zeros. Those revisions were never released
and never reached `main`, so no snapshot an operator can hold predates the
fields; a snapshot taken from one of those revisions must be retaken.

Upgrade the readers before the supervisor. A release that predates version 2
treats the store as a version mismatch and moves it aside, so archive or
migrate the history before a downgrade; the new code never rewrites an old
document merely to read it.

### Upgrading an active file at restart

The writer appends only the format it emits, so a restart settles an active
file whose header names an older version before the first append:

- A segmented store seals the old active file through the normal sealing path.
  Its rows are kept together under the header they were written with, one
  sequence number is claimed, and the new active file starts with a current
  header. No history is dropped, and the sealed source remains readable.
- The legacy single-file store compacts in place, which rewrites the document
  with a current header while keeping its rows.
- If sealing or compaction fails, recording refuses appends rather than writing
  a current row beneath the old header, and reports the save failure.

This is why a downgrade is a clean failure. A version 1 reader now meets a
version 2 header first and reports `version_mismatch`, which moves the store
aside for an operator. Before this rule, a version 1 header could stay in place
while version 2 rows were appended beneath it, and that reader reported the new
row as `corrupt` — indistinguishable from damage to the history the store
really holds.

The first restart after an upgrade therefore creates one extra closed segment
in segmented mode. Account for that segment when budgeting archive runs, and
expect the sequence counter to advance by one even if the store was quiet.

## Upgrade steps

1. Install the release that contains the feature, but leave the configuration
   unchanged. Every journal reader on the host must understand the new event
   vocabulary before the supervisor can emit it, so upgrade readers first.

2. Restart the supervisor gracefully so the new code is loaded while recording
   remains off:

   ```bash
   build-limits-status
   systemctl --user restart buildlimits-supervisor.service
   ```

3. Confirm that `build-limits-status` still reports capacity, held tokens and
   the queue, and that it adds no `cache outcomes` line. The section is absent
   because recording is off, which is the expected state before opting in.

4. Add the option to `~/.config/buildlimits/supervisor.toml`:

   ```toml
   cache_outcomes = "record"
   ```

5. Restart the supervisor gracefully again, then confirm the new line:

   ```bash
   build-limits-status
   ```

   The line reads `cache outcomes recording: N attributed, M readings`. Once a
   reading or outcome has closed it also names the last reason, which is the
   first thing to check while diagnosing missing attribution.

6. When the first cached requests have completed, confirm that history is
   being written and that replay can read it:

   ```bash
   build-limits-metrics --since 1d --max-records 100000
   build-limits-cache-replay h3
   ```

   Replay is read-only. H2 and H3 report their covered span and sample limits
   with every verdict, and an inconclusive verdict is a normal early result.

## Rollback and downgrade

1. Set `cache_outcomes = "off"` and restart the supervisor gracefully. This
   stops new recordings and leaves the existing store in place for a later
   reader.

2. Remove the `cache_outcomes` key before downgrading to a build that predates
   the setting; that build rejects unknown configuration.

3. To discard history as well, stop the supervisor and delete
   `cache-outcomes.jsonl`, `cache-outcomes.jsonl.bad` and
   `cache-outcomes.salt`, then start it again. Deleting the salt means future
   keys no longer join to any history kept elsewhere.

A corrupt or version-mismatched store is moved to `cache-outcomes.jsonl.bad`
during recording startup, but only when that backup does not already exist;
otherwise recording remains unavailable. Read-only replay leaves the store
untouched and reports the decoding failure.

A version 2 store written before the label-time fix records an observation time
that may precede the moment its label was really known, and the format version
cannot identify those rows. They stay readable, but they are no longer scored:
the dataset command cannot distinguish a pre-fix row from a fixed one, so it
marks every version 2 row's label time unverified and the benchmark withholds
it. The reports count those labels as `unverified_label_time`, and a history
made only of them reports `insufficient data` rather than scoring an unsound
release time. Version 3 is the first format that states a row's own label-time
provenance, so a history recorded by this release is scored; no historical row
is repaired or re-stamped.

A version 5 row additionally names the one invocation it belongs to, as the
bounded `attempt` within a `supervisor_generation`, and keeps the launch's own
`completed_at` beside the reading's moment. The pair is a correlation key, not
a proof about the label: an exact repeated append is an idempotent duplicate
and a contradictory pair is excluded from scoring rather than resolved by
choosing a side. A version 4 or earlier row states no correlation and is
reported as uncorrelated, which is a measured absence and never a guessed
identity.

### Store and snapshot versions

| Format   | Version | What changed                                                                                |
| -------- | ------- | ------------------------------------------------------------------------------------------- |
| Store    | 2       | `admitted_at`, `outcome_reason` and `outcome_attribution` on an outcome row                 |
| Store    | 3       | `label_time_provenance`, written `verified` by the recorder that dates a row                |
| Store    | 4       | `generation_digest`, a salted key naming the sccache generation a reading observed          |
| Store    | 5       | `completed_at`, `attempt` and `supervisor_generation` on an outcome row                     |
| Snapshot | 3       | `label_observed_verified` on every event, so a row states its label-time proof              |
| Snapshot | 4       | `by_generation` and `labels_by_generation` in the diagnostics, the per-generation breakdown |
| Snapshot | 5       | `completed_at`, `attempt` and `supervisor_generation` on every event                       |

A version 1 or 2 snapshot still loads, with every event's label time treated as
unverified. A version 3 or later snapshot event that omits the marker is
refused, and so is a marker that is not a boolean. A version 3 or later store
row states its provenance exactly: one that omits the marker is corrupt, and a
version 1 or 2 row decodes it as absent, which is unproven rather than false.
The encoder refuses to write a verified row into an older document, because
that document has nowhere to state it.

The generation digest follows the same rule. A version 4 store row states it,
either as a 64-character lowercase digest or as an explicit null for a row no
single generation can be attributed to. A version 1 to 3 row decodes it as
absent, which is unattributed rather than a generation of its own. A snapshot
written before version 4 has no `by_generation` map, and its rows cannot supply
one after the fact: it loads with every outcome row counted under `unrecorded`,
so the report states the dimension as unavailable rather than as a measured
zero. A version 4 snapshot that omits the map is corrupt, because its own
format says the map is there.

## Reading the status line

| Line                                                | Meaning                                                                                                          |
| --------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `cache outcomes recording: ...`                     | Recording is active; counters are live                                                                           |
| `cache outcomes disabled after an internal failure` | Recording stopped after failing to initialize, after an unsupported server version, or after a later shell fault |
| `cache outcomes status unavailable`                 | One status read could not build the snapshot; recording continues                                                |

_Table 2: Outcome-recording status lines and what the operator should do._

The disabled headline includes the last closed reading reason only when the
status snapshot provides it. A recorder that fails to initialize and a shell
fault after startup publish the fixed disabled description without that reason.
The unavailable line is not a recorder failure: a later status read may
succeed, and no restart is needed.
