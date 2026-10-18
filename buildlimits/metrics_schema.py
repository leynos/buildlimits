"""The frozen vocabulary and field-kind catalogue for admission telemetry.

Every measurement the supervisor or adapter may publish is named here, with
the kind that fixes how it is validated: a token from a closed set, a capped
count or duration, or a boolean. :mod:`buildlimits.metrics` owns the boundary
that reads this catalogue; keeping the tables separate means the accepted
vocabulary can be reviewed as data, without the validation logic around it.

Nothing here imports anything, so a new measurement is added by editing one
table and nothing else.
"""

MAX_COUNTER = 10**12
EVENTS = frozenset({
    "supervisor_started",
    "supervisor_stopped",
    "request_queued",
    "request_admitted",
    "request_finished",
    "server_drain_released",
    "pool_failure",
    "health_changed",
    "internal_error",
    "cache_reachability",
    "cache_load_unknown",
    "adapter_finished",
    "accept_pressure",
    "drain_progress",
    "cache_recovery",
})
OUTCOMES = frozenset({
    "completed",
    "failed",
    "signalled",
    "unknown",
    "refused",
    "cancelled",
    "exec_failed",
    "connect_timeout",
    "supervisor_lost",
    "interrupted",
})
REASONS = frozenset({
    "none",
    "bad_request",
    "attributes",
    "descriptors",
    "unhealthy",
    "launch_failed",
    "exec_failed",
    "other",
    "busy",
})
VOCABULARIES = {
    "outcome": OUTCOMES,
    "reason": REASONS,
    "cancellation": frozenset({"none", "disconnect", "health"}),
    "cache_path": frozenset({"direct", "cached", "direct_fallback"}),
    "health": frozenset({"healthy", "unhealthy", "stopping"}),
    "operation": frozenset({"read", "return", "device"}),
    "accept_state": frozenset({"paused", "resume_failed", "resumed"}),
    "accept_errno": frozenset({"emfile", "enfile", "none"}),
    "drain_operation": frozenset({
        "terminate",
        "escalate",
        "empty_check",
        "accounted_cache",
        "control_loop",
    }),
    "drain_state": frozenset({"pending", "completed", "failed", "recovered"}),
    "drain_reason": frozenset({"none", "live_work", "os_error", "control_error"}),
    # Mirrors ``cache_recovery.Phase`` so a stuck transaction appears in telemetry.
    "phase": frozenset({"idle", "quiescing", "stopping", "accounting", "restarting"}),
}
DURATIONS = frozenset({"wait_seconds", "runtime_seconds", "accept_pause_seconds"})
COUNTERS = frozenset({
    "queued",
    "held",
    "running",
    "draining",
    "server_drain",
    "capacity",
    "connection_attempts",
    "failures",
    "released",
    "fd_retained",
    "fd_budget",
    "fd_high_water",
    "queued_high_water",
    "busy_retries",
    "drain_attempts",
    "drain_failures",
    "drain_recoveries",
    "recovery_attempts",
    "recovery_failures",
    "recovery_exhaustions",
    "recovery_completions",
    "recovery_stale_openings",
    "recovery_roots_deadline_stops",
})
BOOLEANS = frozenset({"reachable", "load_unknown", "fd_budget_known", "accept_paused"})
FIELD_KINDS = {
    **dict.fromkeys(VOCABULARIES, "token"),
    **dict.fromkeys(COUNTERS | DURATIONS, "counter"),
    **dict.fromkeys(BOOLEANS, "boolean"),
}
