# Migrating to cache-outcome recording

This guide is for operators moving a host from a buildlimits release that
cannot record cache outcomes to one that provides the feature. Recording is
opt-in, observes cached compilations without changing admission, and writes a
private local store. The upgrade order below keeps every journal reader ahead
of the vocabulary the supervisor can emit.

The design and its thresholds are recorded in
[ADR 005](adr-005-cache-outcome-recording.md); the operator-facing behaviour is
described under [Record cache outcomes](users-guide.md#record-cache-outcomes).

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
