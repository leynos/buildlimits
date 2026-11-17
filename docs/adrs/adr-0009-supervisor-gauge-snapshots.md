# Architectural decision record (ADR) 009: periodic supervisor gauge snapshots

## Status

Accepted on 2026-11-15. Supervisor-wide counters move from every metric record
to a periodic `gauges` event, `request_queued` is no longer written, and the
report's queue mean becomes time-weighted. This record amends the upstream
compile-admission metrics decision, dev-env-rocky ADR 013, by name; that
document was not inspected for this change.

## Date

2026-11-15.

## Context and problem statement

Every supervisor metric record carried a snapshot of the same supervisor-wide
counters: queue depth, held and running tokens, drain state, capacity and the
descriptor budget. `PressureMetrics` enriched each event on its way to the
journal, so a per-request `request_admitted` or `request_finished` record
repeated state that describes the whole supervisor. The supervisor also wrote a
separate `request_queued` record for every enqueued request.

At the workload measured for issue #56, one hour produced 35,094
`request_queued` records alone. Those records were pure duplication: no report
aggregate reads `request_queued`, and the queue depth it carried is the same
depth the next admission or transition record reports. After #55 quieted
routine supervisor lines, this duplication was the largest remaining source of
journal volume.

The report also needed to keep its existing observable contract. It computes
`queue_maximum` and `held_maximum` from every supervisor record that carries
`queued` or `held`, and a transient peak between two records is invisible. The
new design therefore has to retain sampled maxima, not only current values, and
it has to keep working for windows that mix records written before this change
with records written after it.

## Decision drivers

- Remove record volume that no aggregate consumes.
- Keep the sampled queue and held maxima an operator sees today.
- Compute the queue mean over time rather than over records, because intervals
  between snapshots need not be equal.
- Keep telemetry failure-isolated from admission, tokens and control-fault
  counts.
- Let an upgraded reader read old and new records before any new writer emits
  them.
- Use the supervisor's injected monotonic clock, never journal timestamps.

## Requirements

### Functional requirements

- A periodic `gauges` event carries the current supervisor-wide snapshot, the
  peaks observed since the previous snapshot, the interval it covers and the
  producer-side integral of queue depth over that interval.
- `request_admitted` and `request_finished` carry no supervisor-wide fields,
  including on the pre-registration refusal path.
- `request_queued` is no longer written, but remains a registered event so that
  readers accept history that contains it.
- The report keeps `queue_maximum` and `held_maximum` across both formats, adds
  a time-weighted queue mean and its coverage, and restricts the deprecated
  arithmetic mean to non-`gauges` records.

### Technical requirements

- Observation and delivery run inside a local exception boundary in the
  supervisor tick and cannot change admission, token accounting or the
  control-fault count.
- The producer takes time only from the injected `Clock`.
- A failed emission retains its peaks and integral and merges them into the next
  interval.
- The event and its fields are registered in schema 1 before any producer emits
  them.

## Options considered

### Option A: fixed-interval snapshots without peaks

A periodic record alone loses a queue that rises and falls between two ticks.
The sampled maxima an operator relies on would silently shrink. Rejected.

### Option B: reader-side integration between journal timestamps

Dividing queue depth over gaps between journal timestamps needs policies for
missing timestamps, tied timestamps, restarts and window edges. A window record
has no absolute edges, no pre-window baseline and no restart identifier.
Rejected.

### Option C: producer-side integration with interval peaks

Each `gauges` record carries `interval_seconds` and `queued_seconds`, the
integral of queue depth over the interval since the previous successful
emission. It also carries the peaks observed at the points the supervisor
already samples. The reader divides the summed integral by the summed interval.
Chosen.

## Decision outcome / proposed direction

### D1: the `gauges` event

`gauges` is a supervisor-only event. A record carries:

- the latest snapshot from `gauge_fields`: `queued`, `held`, `running`,
  `draining`, `server_drain` and `capacity`;
- the descriptor snapshot: `fd_retained`, `fd_budget`, `fd_high_water`,
  `fd_budget_known` and `accept_paused`;
- the lifetime queue high-water mark `queued_high_water`;
- the interval peaks `queued_peak` and `held_peak`;
- `interval_seconds`, the monotonic time since the previous successful
  emission, and `queued_seconds`, the integral of `queued` over it.

Missing values stay absent. The producer never substitutes zero for a field it
could not collect.

### D2: cadence, step and spacing

Use module constants rather than configuration:

- `GAUGE_INTERVAL_SECONDS = 30.0` is the heartbeat cadence.
- `GAUGE_STEP = 4` is the change in a count gauge that triggers an earlier
  record.
- `GAUGE_MIN_SPACING_SECONDS = 5.0` is the minimum spacing between
  step-triggered records.

The first flush writes a baseline record with a zero interval. Later flushes
write when the interval has elapsed, when a step change is due and the minimum
spacing has passed, or when shutdown forces a final record. The worst case is
about 840 `gauges` records per hour.

### D3: peak sampling points

Update `queued_peak` and `held_peak` at every point where the supervisor
already samples behaviour: each wrapped metric emission, the observation taken
before an admission effect runs, and every tick. This samples at the same
points the old report used, so the maxima match current behaviour without
adding records.

The admission effect boundary is the important one. A token removes a request
from the queue and the effect it produces then places the group, which blocks
on the systemd scope call. Folding the depth at the boundary, before any
handler runs, keeps the open interval from integrating a depth that has already
changed for as long as placement takes. Folding afterwards biases the queue
mean upwards by the placement latency.

### D4: producer-side integration

On each observation, add the previous queue depth multiplied by the monotonic
elapsed time to `queued_seconds`, using the previous observation as a
piecewise-constant value. A successful flush resets the integral and the peaks
to the current values. A failed emission leaves them in place so the next
interval merges both. The reader computes
`sum(queued_seconds) / sum(interval_seconds)` over `gauges` records that carry
both fields and a positive interval, rounded to three decimals. It computes
`gauge_coverage_seconds` as the sum of every present `interval_seconds`. That
sum includes intervals with no integral, which contribute nothing to the mean,
so it is not by itself the coverage of the mean and the report documentation
says so. A record whose interval starts before the lookback counts wholly,
because the reader has no absolute window edges.

### D5: explicit gauges on rare events

Lifecycle and transition producers keep their explicit, event-time gauges:
`cache_recovery`, `server_drain_released`, `cache_reachability`,
`cache_load_unknown`, `pool_failure`, `health_changed`, `internal_error`,
`supervisor_started` and `supervisor_stopped`. Those events are rare, and their
snapshot describes the transition they record.

### D6: removal and deprecation

`request_queued` is no longer written; it stays in `EVENTS` so readers accept
existing history. `queue_sample_mean` keeps its arithmetic formula for one
release over non-`gauges` records and is marked deprecated in the report's
documentation. `queue_time_weighted_mean` and `gauge_coverage_seconds` are the
replacement.

### D7: volume

The change removes 35,094 `request_queued` records per hour at the measured
workload and adds at most 840 `gauges` records. Against supervisor-unit
structured records at the same workload, measured after #55, the reduction is
about 33%. Against the 277,233 total records measured for the ticket, the
removal is 12.7% and `gauges` adds back at most 0.3 percentage points.

The measured test compares the two producers over one workload: the per-request
record count of the legacy producer, including the enqueue records it wrote,
against the new count, which excludes them and adds the snapshots. That is the
same subtraction as the busy-hour figures above, and the busy-hour replay keeps
the 13% floor a lower bound rather than a fitted result.

### D8: reader-before-writer ordering

Ship and deploy the reader first. It accepts both formats and computes maxima
across them. Only then may a supervisor emit `gauges`. An older reader drops
`gauges` entirely, because the event is not in its catalogue, so a new window
read by an old reader shows only the gauge samples on transition events.

## Goals and non-goals

### Goals

- Remove per-record duplication of supervisor-wide counters.
- Preserve the sampled queue and held maxima.
- Make the queue mean time-weighted and disclose its coverage.
- Keep mixed old and new windows readable.

### Non-goals

- Change compiler admission, token accounting or recovery behaviour.
- Add configuration for the snapshot cadence.
- Remove `request_queued` from the accepted event catalogue.
- Change the history, truncation or request-rate logic.

## Migration plan

1. Release the reader change: schema registration, report aggregation and
   documentation. It changes no producer, but buildlimits ships as one
   distribution, so there is no separate installable reader-only artefact.
2. Upgrade every journal reader on the host before the supervisor restarts.
   Stage the release into its own `UV_TOOL_DIR` and `UV_TOOL_BIN_DIR` so the new
   `build-limits-metrics` can be verified against the live legacy journal
   while the running supervisor and the active installation keep the old build.
   The migration guide carries the commands and the expected legacy-only report.
3. Install the release into the active installation and restart the supervisor
   that emits `gauges` and stops writing `request_queued`.
4. Confirm the report's `queue_time_weighted_mean` and
   `gauge_coverage_seconds` appear for new windows, and that maxima still match
   the previous behaviour.

## Known risks and limitations

- Snapshot records can be lost. Coverage exposes the loss instead of
  distributing it silently over the surviving records.
- A forced final flush is best-effort; a failing transport can still lose the
  last interval.
- The deprecation window for `queue_sample_mean` means two queue means appear
  in one report. Their names and the coverage figure keep the difference
  explicit.
- An old reader reading a new window sees no periodic maxima or mean until its
  readers are upgraded.

## Architectural rationale

The supervisor owns the monotonic clock and the admission state, so it is the
only component that can integrate queue depth without inventing window edges or
using journal timestamps. Keeping the snapshot behind the metrics port
preserves the hexagonal boundary: the accumulator and report are pure, and the
existing failure-isolated transport remains the only path to the journal.

## References

- Issue #56, "Replace per-record supervisor counters and `request_queued` with a
  periodic snapshot".
- [ADR 003](adr-0003-bounded-restart-tracing.md), the bounded metrics schema
  this record extends.
- dev-env-rocky ADR 013, the upstream compile-admission metrics decision this
  record amends by name.
