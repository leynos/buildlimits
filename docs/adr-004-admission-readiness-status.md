# Architectural decision record (ADR) 004: expose admission readiness in status

## Status

Accepted on 2026-10-25. Report admission readiness separately from supervisor
health and cache reachability, and provide an opt-in CLI exit contract for
readiness checks.

## Date

2026-10-25.

## Context and problem statement

Supervisor health answers whether the status process and token pool are
healthy. Cache reachability answers whether the managed cache can be reached.
Neither answers whether admission can make progress.

On 2026-10-10, a status query on rohga returned success and `healthy: true`
while every slot was retained in `server-drain`, no compiler roots were visible
under a live sccache service, and operator recovery was required. The queued
request was admitted 664 seconds later, after an authorized cache-service
drain. This observation shows that the status contract could report a healthy
supervisor while admission was unable to make progress and required operator
action. It does not show that any particular monitoring consumer declared the
host ready.

## Decision drivers

- Let provisioning and monitoring distinguish process health from usable
  admission.
- Keep normal capacity saturation and progressing recovery distinct from a
  queue that needs operator action.
- Preserve cache-fallback operation when sccache is unreachable.
- Keep readiness checks observational and preserve retained-token accounting.
- Preserve existing callers that use status only to check supervisor health.

## Decision

Add an `admission` object to the status snapshot with `state`, `ready`, a
count-only `reason`, and `running`, `draining`, `server_drain` and `queued`
counts. Its `queued` count comes from the admission queue. The classifier is
pure and read-only: it uses the health and closing flags, an authoritative
ledger snapshot, the immediate drain-fault kind, and recovery outlook fields
`automatic`, `phase`, `attempts` and `pending`. It does not depend on cache
reachability or rendered recovery guidance.

Apply this precedence: `unhealthy`, `closing`, `blocked`, `draining`,
`saturated`, then `ready`. Only `ready`, `saturated` and `draining` set
`admission.ready` to true. `blocked` means all capacity is retained in
server-drain and operator recovery is required. `draining` means all capacity
is retained in server-drain and the snapshot shows a current progress path; it
is not a promise of eventual progress. Full capacity with ordinary compiler
work is `saturated`, and partial server-drain retention remains `ready` or
`saturated` according to available capacity.

Keep the default `build-limits-status` exit status health-only. Add an opt-in
`--ready` flag for plain and JSON output, without changing the output content:

| Exit status | Default behaviour           | With `--ready`                                  |
| ----------- | --------------------------- | ----------------------------------------------- |
| `0`         | Query succeeded and healthy | Healthy and `admission.ready` is exactly `true` |
| `1`         | Query or health failure     | Query or non-truthy health failure              |
| `3`         | Not used                    | Readiness false, missing or malformed           |

_Table 1: `build-limits-status` exit statuses by mode._

Recovery guidance remains an operational diagnostic. It can be delayed until
drain attempts reach their threshold and can change as automatic recovery
becomes pending or stops. It is independent of the readiness classification,
which uses the immediate fault and current recovery outlook.

## Consequences

- Status consumers can check admission availability without treating
  `healthy`, successful status transport or cache reachability as readiness.
- A live cache fault with pending automatic recovery can be `draining`; a full
  server-drain with no progress path and required operator action is `blocked`.
- Readiness failure never authorizes releasing retained tokens or stopping
  sccache. Only verified stopped-and-empty accounting releases those tokens.
- Status is a point-in-time snapshot. It cannot promise that a current progress
  path will finish or establish what a monitoring consumer observed or decided.
- Existing health-only callers retain their default exit semantics and can
  continue using status for process-health checks.

## References

- [Issue 23: expose blocked compile admission in readiness
  checks](https://github.com/leynos/buildlimits/issues/23)
