# Migrating to periodic gauge snapshots

This guide is for operators moving a host from a buildlimits release that
attached supervisor-wide counters to every metric record to one that publishes
them as a periodic `gauges` event. The upgrade order keeps every journal reader
ahead of the vocabulary the supervisor can emit.

The decision and its field definitions are recorded in
[ADR 009](adrs/adr-0009-supervisor-gauge-snapshots.md).

## Who should read this

Read this guide if you run `build-limits-metrics`, ship journal records to
another system, or have written a reader that consumes `BUILD_LIMITS_*` fields.
If you only run the supervisor and never read its metrics, the runtime
behaviour you see in status is unchanged.

## What changes

| Area          | Before                                | With periodic snapshots                         |
| ------------- | ------------------------------------- | ----------------------------------------------- |
| Journal       | Counters on every supervisor record   | Counters on `gauges` and rare transition events |
| Queue records | One `request_queued` per enqueued job | None; the event is still accepted when read     |
| Queue maxima  | Sampled from every record             | Snapshots carry interval peaks as well          |
| Queue mean    | Arithmetic mean of samples            | Time-weighted mean with a coverage figure       |
| Reader order  | Any order                             | Every reader before the supervisor              |

_Table 1: Behaviour changed by periodic gauge snapshots._

`request_finished` and `request_admitted` still carry their request-level
fields: outcome, reason, cancellation, cache path, wait time and runtime. They
no longer repeat queue depth, token counts, capacity or the descriptor budget.
`request_queued` remains a registered event, so old history is still read, but
no new release writes it.

## Upgrade steps

1. Install the release that contains the reader change. It registers `gauges`
   and reads both old and new records. Upgrade every journal reader on the host
   before the supervisor that emits the new event.

   buildlimits ships one distribution, so there is no separate reader-only
   package: installing the new release on the host also places the new
   supervisor executable. Stage it into its own tool directory first. That
   leaves both the active installation and the running supervisor untouched,
   while a new reader becomes available to check the live journal:

   ```bash
   UV_TOOL_DIR="$HOME/.local/share/buildlimits-staging/tool" \
   UV_TOOL_BIN_DIR="$HOME/.local/share/buildlimits-staging/bin" \
   uv tool install --python 3.14 git+https://github.com/leynos/buildlimits
   ```

   The tool-directory variables are read only for that command, so the install
   under `~/.local/share/buildlimits-staging` does not become the active
   `build-limits-supervisor` or `build-limits-metrics`. Remove the directory
   once the upgrade is complete.

2. Verify the staged reader against the existing journal:

   ```bash
   "$HOME/.local/share/buildlimits-staging/bin/build-limits-metrics" --since 1h
   ```

   Existing windows keep their maxima. `queue_time_weighted_mean` and
   `gauge_coverage_seconds` are `null` for a legacy-only window, because no
   record carries a producer-side interval yet. That is the expected result,
   not a failure, and proves the new reader accepted the old format before the
   supervisor writes anything new.

3. Install the release that emits `gauges` into the active installation and
   restart the supervisor gracefully:

   ```bash
   build-limits-status
   systemctl --user restart buildlimits-supervisor.service
   ```

4. Confirm that a new window reports the new fields:

   ```bash
   build-limits-metrics --since 10m
   ```

   A window that contains `gauges` records shows the snapshot values and
   `gauge_coverage_seconds`. The maxima continue to match the peaks the
   supervisor observed, including peaks that rose and fell between two
   snapshots.

   Wait for a snapshot with a positive `interval_seconds` before expecting a
   numeric `queue_time_weighted_mean`. Immediately after the restart the only
   snapshot is the baseline, which claims a zero interval and therefore has no
   coverage and no mean. That window is a successful upgrade, not a failure.
   The mean also needs a queue integral: a snapshot whose interval contained an
   unread depth publishes none, so the mean appears with the next interval that
   was measured throughout.

## Reading a mixed window

A window that spans the upgrade contains legacy enriched records and new
`gauges` records. The report combines both:

- `queue_maximum` and `held_maximum` take the larger of the legacy samples and
  the new current and peak values, so the maxima never fall across the upgrade.
- `queue_time_weighted_mean` uses only `gauges` records that carry both integral
  fields and a positive interval. Journal timestamps are never used for
  weighting.
- `gauge_coverage_seconds` sums the intervals the snapshots actually cover,
  including intervals that carry no queue integral because a depth read failed.
  Those intervals do not contribute to the mean, so the figure describes what
  the snapshots span rather than what the mean measured. Do not use it alone as
  the measured coverage of `queue_time_weighted_mean`. A low coverage next to a
  long window means records were lost or the supervisor restarted, not that the
  queue was empty.
- `queue_sample_mean` is deprecated. It keeps the old arithmetic formula over
  non-`gauges` records for one release, so an existing reader keeps its named
  value while it migrates.

A record whose interval starts before the lookback is counted wholly, because
the reader has no absolute window edges and no pre-window baseline.

## Older readers

An older reader drops `gauges` entirely: the event is not in its catalogue, so
the record is rejected rather than reported. A new window read by an old reader
therefore shows only the gauge samples attached to transition events such as
health changes and supervisor start-up. It does not show periodic queue maxima
or the time-weighted mean. Upgrade every reader before the supervisor that
emits the event.

## Rollback and downgrade

1. Restart the supervisor from the previous release. It resumes writing
   `request_queued` and per-record counters, and stops writing `gauges`.

2. Keep the upgraded reader. It reads the old format as it always did.

3. Do not downgrade a reader while the supervisor still emits `gauges`. The
   older reader will ignore those records, and the window's maxima and mean
   will silently narrow to the transition samples.

No configuration key was added, so no configuration change is needed in either
direction.
