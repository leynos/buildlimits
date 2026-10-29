"""Fixed status documents the summary tests render.

They are a table rather than inline literals so each variant shows the fields
it adds and the line it must produce; the snapshot still pins the whole
summary an operator reads.
"""

_WAITING = {
    "request": 7,
    "label": "serde",
    "directory": "<project>",
    "waiting_seconds": 12.5,
}
_RUNNING = {
    "group": "g3",
    "request": 5,
    "state": "running",
    "cached": True,
    "label": "tokio",
    "directory": "<project>",
    "running_seconds": 40.0,
}

STATES: dict[str, tuple[dict[str, object], str]] = {
    "descriptors": (
        {
            "healthy": True,
            "descriptors": {
                "open": 1277,
                "soft_limit": 524288,
                "hard_limit": 524288,
                "sampling_errno": None,
            },
        },
        "open descriptors 1277, soft limit 524288, hard limit 524288",
    ),
    "unavailable-descriptors": (
        {
            "healthy": True,
            "descriptors": {
                "open": None,
                "soft_limit": 524288,
                "hard_limit": 524288,
                "sampling_errno": 24,
            },
        },
        "open descriptors unavailable (errno 24)",
    ),
    "unlimited-descriptors": (
        {
            "healthy": True,
            "descriptors": {
                "open": 12,
                "soft_limit": -1,
                "hard_limit": -1,
                "sampling_errno": None,
            },
        },
        "open descriptors 12, soft limit unlimited, hard limit unlimited",
    ),
    "cached": (
        {
            "capacity": 4,
            "held": 0,
            "healthy": True,
            "caching": True,
            "cache_mode": "sccache",
        },
        "capacity 4, held 0, healthy, caching through sccache",
    ),
    "blocked-admission": (
        {
            "capacity": 4,
            "held": 4,
            "healthy": True,
            "caching": True,
            "admission": {
                "state": "blocked",
                "ready": False,
                "reason": "4 server-drain slots retained; operator recovery required",
                "running": 0,
                "draining": 0,
                "server_drain": 4,
                "queued": 1,
            },
        },
        "admission blocked: 4 server-drain slots retained; operator recovery required",
    ),
    "draining-admission": (
        {
            "capacity": 4,
            "held": 4,
            "healthy": True,
            "caching": False,
            "cache_mode": "sccache",
            "admission": {
                "state": "draining",
                "ready": True,
                "reason": "4 server-drain slots retained; recovery is in accounting",
                "running": 0,
                "draining": 0,
                "server_drain": 4,
                "queued": 1,
            },
        },
        "admission draining: 4 server-drain slots retained; recovery is in accounting",
    ),
    "closing-admission": (
        {
            "capacity": 4,
            "held": 0,
            "healthy": True,
            "caching": True,
            "admission": {
                "state": "closing",
                "ready": False,
                "reason": "supervisor is closing",
                "running": 0,
                "draining": 0,
                "server_drain": 0,
                "queued": 0,
            },
        },
        "admission closing: supervisor is closing",
    ),
    "unhealthy-admission": (
        {
            "capacity": 4,
            "held": 1,
            "healthy": False,
            "caching": True,
            "admission": {
                "state": "unhealthy",
                "ready": False,
                "reason": "supervisor is unhealthy",
                "running": 1,
                "draining": 0,
                "server_drain": 0,
                "queued": 0,
            },
        },
        "admission unhealthy: supervisor is unhealthy",
    ),
    "legacy-missing-admission": (
        {"capacity": 4, "held": 0, "healthy": True, "caching": True},
        "capacity 4, held 0, healthy, caching through sccache",
    ),
    "legacy-non-dictionary-admission": (
        {
            "capacity": 4,
            "held": 0,
            "healthy": True,
            "caching": True,
            "admission": "not a structured section",
        },
        "capacity 4, held 0, healthy, caching through sccache",
    ),
    "restarting": (
        {
            "capacity": 4,
            "held": 2,
            "healthy": True,
            "restarting": True,
            "caching": True,
            "cache_mode": "sccache",
            "queued": [_WAITING],
            "groups": [_RUNNING],
        },
        "healthy, restarting once running groups finish, caching through sccache",
    ),
    "unreachable-cache": (
        {
            "capacity": 4,
            "held": 0,
            "healthy": True,
            "caching": False,
            "cache_mode": "sccache",
        },
        "sccache unreachable, compiling directly",
    ),
    "direct": (
        {
            "capacity": 4,
            "held": 0,
            "healthy": True,
            "caching": False,
            "cache_mode": "direct",
        },
        "capacity 4, held 0, healthy, compiling directly",
    ),
    "unhealthy": (
        {
            "capacity": 4,
            "held": 1,
            "healthy": False,
            "caching": True,
            "cache_mode": "sccache",
        },
        "UNHEALTHY",
    ),
    "health-reason": (
        {
            "capacity": 4,
            "held": 1,
            "healthy": False,
            "caching": True,
            "cache_mode": "sccache",
            "health_reason": "internal error; draining before exit",
        },
        "unhealthy reason: internal error; draining before exit",
    ),
    "drain-fault": (
        {
            "capacity": 4,
            "held": 1,
            "healthy": False,
            "caching": True,
            "cache_mode": "sccache",
            "drain_fault": "manager query failed: connection refused",
            "groups": [{**_RUNNING, "state": "server-drain"}],
        },
        "drain check failed; tokens retained: manager query failed: connection refused",
    ),
    "drain-fault-kind": (
        {
            "capacity": 4,
            "held": 1,
            "healthy": False,
            "caching": True,
            "cache_mode": "sccache",
            "drain_fault_kind": "live_work",
            "drain_fault": "retained detached work: live sccache service",
        },
        "drain fault kind: live_work",
    ),
    "live-work-guidance": (
        {
            "capacity": 4,
            "held": 1,
            "healthy": False,
            "caching": True,
            "cache_mode": "sccache",
            "drain_fault_kind": "live_work",
            "drain_fault": "retained detached work: live sccache service",
            "recovery_action": "Run build-limits-recover to stop the managed cache.",
        },
        "drain fault kind: live_work",
    ),
    "recovery-action": (
        {
            "capacity": 4,
            "held": 1,
            "healthy": False,
            "caching": True,
            "cache_mode": "sccache",
            "recovery_action": "restore manager query transport; do not release tokens",
        },
        (
            "required recovery action: restore manager query transport; "
            "do not release tokens"
        ),
    ),
    "missing-group-label": (
        {
            "capacity": 1,
            "held": 1,
            "healthy": False,
            "caching": True,
            "cache_mode": "sccache",
            "groups": [{**_RUNNING, "state": "server-drain", "label": None}],
        },
        (
            "g3 server-drain (cached): request metadata unavailable; "
            "retaining compiler token"
        ),
    ),
    "missing-group-directory": (
        {
            "capacity": 1,
            "held": 1,
            "healthy": False,
            "caching": True,
            "cache_mode": "sccache",
            "groups": [{**_RUNNING, "state": "server-drain", "directory": None}],
        },
        (
            "g3 server-drain (cached): request metadata unavailable; "
            "retaining compiler token"
        ),
    ),
    "missing-group-duration": (
        {
            "capacity": 1,
            "held": 1,
            "healthy": True,
            "caching": True,
            "cache_mode": "sccache",
            "groups": [{**_RUNNING, "running_seconds": None}],
        },
        "g3 running (cached): tokio in <project>, duration unavailable",
    ),
    "queued": (
        {
            "capacity": 1,
            "held": 1,
            "healthy": True,
            "caching": True,
            "cache_mode": "sccache",
            "queued": [_WAITING],
        },
        "waiting 12.5s: serde in <project>",
    ),
    "recovering": (
        {
            "capacity": 1,
            "held": 1,
            "healthy": True,
            "caching": True,
            "cache_mode": "sccache",
            "cache_recovery": {
                "phase": "stopping",
                "attempts": 2,
                "last_fault": "cannot stop the managed cache",
                "retry_in": 60.0,
            },
        },
        "cache recovery stopping: next attempt in 60.0s, attempts 2",
    ),
    "recovered": (
        {
            "capacity": 1,
            "held": 0,
            "healthy": True,
            "caching": True,
            "cache_mode": "sccache",
            # A finished transaction renders no recovery line: the summary an
            # operator reads after a successful recovery is the healthy one.
            "cache_recovery": {
                "phase": "idle",
                "attempts": 0,
                "last_fault": None,
                "retry_in": 0.0,
            },
        },
        "capacity 1, held 0, healthy, caching through sccache",
    ),
    "group": (
        {
            "capacity": 1,
            "held": 1,
            "healthy": True,
            "caching": True,
            "cache_mode": "sccache",
            "groups": [_RUNNING],
        },
        "g3 running (cached): tokio in <project>, 40.0s",
    ),
}
