# Architectural decision record (ADR) 003: bounded restart tracing

## Status

Accepted on 2026-10-20. Use OpenTelemetry spans for scope placement and
supervisor-restart retries, exported as bounded native journal records.

## Date

2026-10-20.

## Context and problem statement

PR 21 fixes a restart that interrupts a transient-scope placement. Its metrics
show aggregate restart retries and supervisor health changes, but do not show
the duration and outcome of one placement or retry episode. The configured PR
observability check requires traces at process boundaries. Tracing must remain
local to the host, avoid request data, and never change compiler admission.

## Decision Drivers

- Record the placement and retry boundaries with actual OpenTelemetry spans.
- Keep the output useful with the existing native journal transport.
- Exclude compiler payloads, paths, credentials, raw errors and user-provided
  identifiers.
- Make SDK and exporter failures observational only.

## Options considered

### Option A: keep the existing metrics

The existing counters remain useful for aggregate rates and health transitions,
but cannot provide per-operation elapsed time and outcomes. The configured
observability check also does not accept them as traces. Rejected.

### Option B: export OpenTelemetry spans over OTLP

The [OpenTelemetry Python instrumentation guide] and [exporter guide] recommend
standard SDK instrumentation and OTLP exporters for applications that have a
collector. This service has no collector contract or endpoint configuration;
adding one would create a network dependency and an operator migration
unrelated to the restart fix. Deferred until the project has a collector to
target.

### Option C: export bounded spans to journald

Use the OpenTelemetry SDK to create spans and a small synchronous exporter to
write their bounded fields through the existing non-blocking native journal
socket. Chosen.

## Decision outcome / proposed direction

The supervisor records one `scope_placement` span for each placement. The
adapter starts `adapter_restart_retry` only after it encounters a restart and
ends that span at admission, retry exhaustion, failure or cancellation. The
adapter initializes the SDK lazily. The exporter records operation, elapsed
milliseconds, outcome, failure category, stop mode, retry attempt and generated
fixed-width trace identifiers. It accepts only fixed vocabularies and capped
numeric values, and drops telemetry failures without affecting the operation.

Trace records use `BUILD_LIMITS_TRACE_*` fields and are queried directly with
`journalctl`; `build-limits-metrics` continues to read metric events only.

## Goals and non-goals

### Goals

- Explain how a restart affected one scope placement and its subsequent retry.
- Keep trace output bounded, local and independent of compiler input.

### Non-goals

- Send traces to a remote collector or provide distributed trace storage.
- Replace the aggregate metric schema or its reporting command.

## Known risks and limitations

- Journald rate limits and retention can lose trace records.
- Trace identifiers correlate spans only when a future consumer reads these
  journal fields; no current reporting command reconstructs traces.
- The OpenTelemetry SDK is a new runtime dependency. The adapter imports it
  only when a restart retry needs a span.

## Architectural rationale

The SDK supplies standard span lifecycle and identifiers, while the exporter
uses the existing non-blocking output boundary. Fixed attributes keep the trace
schema separate from request state and let exporter failures remain outside
admission and compiler result handling.

[OpenTelemetry Python instrumentation guide]: https://opentelemetry.io/docs/languages/python/instrumentation/
[exporter guide]: https://opentelemetry.io/docs/languages/python/exporters/
