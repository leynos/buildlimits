"""The frozen vocabulary and field-kind catalogue for admission telemetry.

Every measurement the supervisor or adapter may publish is named here, with
the kind that fixes how it is validated: a token from a closed set, a capped
count or duration, or a boolean. :mod:`buildlimits.metrics` owns the boundary
that reads this catalogue; keeping the tables separate means the accepted
vocabulary can be reviewed as data, without the validation logic around it.

Nothing here imports anything, so accepted token sets stay data-only. The
outcome domain's ``typ.Literal`` aliases mirror the cache-result, outcome,
store, evaluation and gate tables below.
"""

MAX_COUNTER = 10**12
EVENTS = frozenset({
    "supervisor_started",
    "supervisor_stopped",
    # No release writes this event now; it stays accepted so an old window
    # still reads. The queue is described by ``gauges`` instead (ADR 009).
    "request_queued",
    "request_admitted",
    "request_finished",
    "gauges",
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
    "cache_quiesce",
    "cache_outcome",
    "outcome_reading",
    "outcome_store",
    "archive_run",
    "retention_run",
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
    "exempt",
})
REASONS = frozenset({
    "none",
    "bad_request",
    "attributes",
    "descriptors",
    "unhealthy",
    "restarting",
    "launch_failed",
    "exec_failed",
    "other",
    "busy",
})
# Outcome-recording vocabularies are fixed here before their journal events are
# introduced. The pure outcome modules use the same accepted tokens.
CACHE_RESULTS = frozenset({
    "hit",
    "miss",
    "not_cacheable",
    "not_cached",
    "failed",
    "error",
    "declined",
    "not_compile",
    "unsupported",
    "unattributed",
})
OUTCOME_REASONS = frozenset({
    "none",
    "timeout",
    "read_error",
    "parse_error",
    "unsupported_version",
    "generation_unavailable",
    "generation_changed",
    "empty_stats",
    "counter_decreased",
    "ineligible_window",
    "count_mismatch",
    "mixed_window",
    "straddled_reading",
    "supervisor_stopped",
    "no_baseline",
    "window_overflow",
    "recorder_disabled",
})
STORE_REASONS = frozenset({
    "none",
    "corrupt",
    "unreadable",
    "version_mismatch",
    "tag_dropped",
    "salt_unavailable",
    "save_failed",
    "expired",
    "bytes",
    "records",
    "bytes_and_records",
    "expired_and_bytes",
    "expired_and_records",
    "expired_and_bytes_and_records",
    "salt_changed",
    "clock_skew",
    "seal_failed",
    "archive_backlog",
})
EVALUATION_REASONS = frozenset({
    "none",
    "key_unavailable",
    "store_unavailable",
    "ineligible",
})
# Why a published outcome did not become a store row. ``key_unavailable`` names
# a launch whose salt or crate identity was missing, and
# ``label_time_unavailable`` names one with no wall-clock label moment. An
# omitted outcome is measured loss, not a silent gap: the observer sums this
# vocabulary and reconciles it against the events it published. The reason is a
# token on each ``cache_outcome`` event, because one event describes one
# outcome; a count of omissions is derived from the events and is never itself
# published as a counter.
RECORD_OMISSIONS = frozenset({
    "none",
    "key_unavailable",
    "label_time_unavailable",
})
STORE_OPERATIONS = frozenset({"load", "append", "compact", "seal", "salt"})
GATE_REASONS = frozenset({
    "queued",
    "cached_moving",
    "server_drain",
    "unreachable",
    "held",
    "recovering",
    "barrier",
    "in_flight",
    "read_skipped",
    "backing_off",
})
VOCABULARIES = {
    "outcome": OUTCOMES,
    "reason": REASONS,
    "cancellation": frozenset({"none", "disconnect", "health"}),
    "cache_path": frozenset({"direct", "cached", "direct_fallback"}),
    # Which backend an admission was composed with. It is a field of its own
    # rather than a ``cache_path`` value, so ``cached`` and ``direct_fallback``
    # keep their meaning in every mode and one query can count either.
    "cache_backend": frozenset({"direct", "sccache", "kache"}),
    "health": frozenset({"healthy", "unhealthy", "stopping", "restarting"}),
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
    # How a start-up stop of a live cache ended; see ``cache_quiesce``.
    "quiesce_outcome": frozenset({"stopped", "refused", "timed_out"}),
    "cache_result": CACHE_RESULTS,
    "outcome_attribution": frozenset({"solo", "unanimous", "inferred", "none"}),
    "crate_origin": frozenset({"registry", "git", "path", "unknown"}),
    "outcome_reason": OUTCOME_REASONS,
    "store_reason": STORE_REASONS,
    "evaluation_reason": EVALUATION_REASONS,
    "store_operation": STORE_OPERATIONS,
    "gate_reason": GATE_REASONS,
    # The reason one published outcome produced no store row, carried by its
    # ``cache_outcome`` event. It is a token, never a count: the observer sums
    # the closed values to measure the omitted total.
    "record_omitted": RECORD_OMISSIONS,
    "child_sighting": frozenset({"seen", "not_seen", "ambiguous", "unavailable"}),
    # A timer-fired archive run, reported once per firing through
    # ``run_archive``. The statuses mirror ``RunStatus``; the reasons are
    # the closed values a report can carry, with ``none`` for a run that
    # completed or failed without a run-level reason.
    "archive_status": frozenset({
        "ok",
        "failed",
        "corrupt",
        "busy",
        "no_headroom",
        "unsettled",
    }),
    "archive_reason": frozenset({
        "none",
        "busy",
        "no_headroom",
        "unreadable",
        "state_write_failed",
        # A completed pass whose clock baseline could not be recorded. The
        # store is intact; the next firing may withhold age eviction once more
        # than it needed to.
        "clock_baseline_failed",
    }),
}
DURATIONS = frozenset({
    "wait_seconds",
    "runtime_seconds",
    "accept_pause_seconds",
    "quiesce_seconds",
    "interval_seconds",
    "queued_seconds",
})
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
    "queued_peak",
    "held_peak",
    "busy_retries",
    "restart_retries",
    "drain_attempts",
    "drain_failures",
    "drain_recoveries",
    "recovery_attempts",
    "recovery_failures",
    "recovery_exhaustions",
    "recovery_completions",
    "recovery_stale_openings",
    "recovery_roots_deadline_stops",
    "readings",
    "readings_skipped",
    "readings_failed",
    "window_groups",
    "store_records",
    "store_discarded",
    "child_samples",
    "child_samples_taken",
    "child_sample_failures",
    "archived",
    "trimmed",
    "deleted",
    "quarantined",
    "evicted_age",
    "evicted_bytes",
    "evicted_records",
    "evicted_age_and_bytes",
    "evicted_age_and_records",
    "evicted_bytes_and_records",
    "evicted_age_and_bytes_and_records",
    "retained_logical_bytes",
    "retained_records",
    "retained_unmeasured_entries",
})
BOOLEANS = frozenset({"reachable", "load_unknown", "fd_budget_known", "accept_paused"})
# The counters that describe the whole supervisor rather than one request. They
# belong on periodic ``gauges`` snapshots and rare transition events, not on
# every per-request record, so producers and tests can assert the split.
SUPERVISOR_WIDE_FIELDS = frozenset({
    "queued",
    "held",
    "running",
    "draining",
    "server_drain",
    "capacity",
    "fd_retained",
    "fd_budget",
    "fd_high_water",
    "fd_budget_known",
    "accept_paused",
    "queued_high_water",
})
FIELD_KINDS = {
    **dict.fromkeys(VOCABULARIES, "token"),
    **dict.fromkeys(COUNTERS | DURATIONS, "counter"),
    **dict.fromkeys(BOOLEANS, "boolean"),
}
