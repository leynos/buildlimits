"""Closed cache-outcome vocabularies shared by the pure domain modules."""

import typing as typ

type CacheResult = typ.Literal[
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
]
type OutcomeReason = typ.Literal[
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
]
type StoreReason = typ.Literal[
    "none",
    "corrupt",
    "unreadable",
    "version_mismatch",
    "tag_dropped",
    "salt_unavailable",
    "save_failed",
    "expired",
    "salt_changed",
    "clock_skew",
]
type EvaluationReason = typ.Literal[
    "none",
    "key_unavailable",
    "store_unavailable",
    "ineligible",
]
type StoreOperation = typ.Literal["load", "append", "compact", "salt"]
type GateReason = typ.Literal[
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
]
