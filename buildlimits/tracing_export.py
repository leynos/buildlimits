"""Export bounded tracing records to the native journal transport."""

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

OPERATIONS = frozenset({
    "scope_placement",
    "adapter_restart_retry",
    "statistics_read",
    "admission_selection",
    "outcome_store_load",
    "outcome_store_save",
    "outcome_store_compact",
})
OUTCOMES = frozenset({
    "admitted",
    "restarting",
    "launch_failed",
    "requester_gone",
    "release_failed",
    "retry_exhausted",
    "failed",
    "cancelled",
    "unknown",
    "completed",
})
FAILURE_CATEGORIES = frozenset({
    "none",
    "scope_placement",
    "launch_release",
    "restart_refusal",
    "supervisor_unavailable",
    "adapter_error",
    "requester_unavailable",
    "cancelled",
    "internal_error",
    "unknown",
    "statistics_query",
    "store_io",
    "salt",
})
STOP_MODES = frozenset({"none", "graceful", "immediate", "unknown"})
MAX_TRACE_COUNT = 1_000_000
MAX_RETRY_ATTEMPT = MAX_TRACE_COUNT
MAX_DURATION_MS = 86_400_000
TRACE_FAILURE_KINDS = ("transport", "invalid_record", "sdk")
TRACE_FAILURES = (
    ArithmeticError,
    AttributeError,
    ImportError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


def _record(span: object, source: str) -> dict[str, str]:
    """Build the fixed journal schema without copying arbitrary span data.

    Returns
    -------
    dict[str, str]
        One span record containing only fixed fields and bounded values.

    Raises
    ------
    ValueError
        If the operation or generated span identifiers are invalid.
    """
    name = getattr(span, "name", "")
    if name not in OPERATIONS:
        msg = "unknown span operation"
        raise ValueError(msg)
    attributes = _bounded_attributes(getattr(span, "attributes", {}) or {})
    start = getattr(span, "start_time", None)
    end = getattr(span, "end_time", None)
    elapsed_ms = (
        0 if start is None or end is None else max(0, (end - start) // 1_000_000)
    )
    context = getattr(span, "context", None)
    trace_id = getattr(context, "trace_id", 0)
    span_id = getattr(context, "span_id", 0)
    for identifier, identifier_kind, width in (
        (trace_id, "trace", 128),
        (span_id, "span", 64),
    ):
        if not _is_valid_identifier(identifier, width):
            msg = f"invalid {identifier_kind} identifier"
            raise ValueError(msg)
    return {
        "BUILD_LIMITS_TRACE_SCHEMA": "1",
        "BUILD_LIMITS_TRACE_SOURCE": source,
        "BUILD_LIMITS_TRACE_EVENT": "span_completed",
        "BUILD_LIMITS_TRACE_OPERATION": name,
        "BUILD_LIMITS_TRACE_OUTCOME": str(attributes.get("outcome", "unknown")),
        "BUILD_LIMITS_TRACE_FAILURE_CATEGORY": str(
            attributes.get("failure_category", "unknown")
        ),
        "BUILD_LIMITS_TRACE_STOP_MODE": str(attributes.get("stop_mode", "unknown")),
        "BUILD_LIMITS_TRACE_RETRY_ATTEMPT": str(attributes.get("retry_attempt", 0)),
        "BUILD_LIMITS_TRACE_DURATION_MS": str(min(MAX_DURATION_MS, elapsed_ms)),
        "BUILD_LIMITS_TRACE_ID": f"{trace_id:032x}",
        "BUILD_LIMITS_TRACE_SPAN_ID": f"{span_id:016x}",
    }


def _bounded_attributes(attributes: cabc.Mapping[str, object]) -> dict[str, str | int]:
    """Keep only fixed categorical fields and a capped retry counter.

    Returns
    -------
    dict[str, str | int]
        Recognized values, with arbitrary attributes omitted.
    """
    bounded: dict[str, str | int] = {}
    vocabularies = {
        "outcome": OUTCOMES,
        "failure_category": FAILURE_CATEGORIES,
        "stop_mode": STOP_MODES,
    }
    for name, vocabulary in vocabularies.items():
        value = attributes.get(name)
        if isinstance(value, str) and value in vocabulary:
            bounded[name] = value
    retry_attempt = attributes.get("retry_attempt")
    if isinstance(retry_attempt, int) and not isinstance(retry_attempt, bool):
        bounded["retry_attempt"] = min(MAX_RETRY_ATTEMPT, max(0, retry_attempt))
    return bounded


def _failure_kind(error: Exception) -> str:
    """Map a trace failure to a fixed category without retaining its details.

    Returns
    -------
    str
        One of ``transport``, ``invalid_record`` or ``sdk``.
    """
    match error:
        case OSError():
            return "transport"
        case ValueError():
            return "invalid_record"
        case _:
            return "sdk"


def _is_valid_identifier(identifier: object, width: int) -> bool:
    """Return whether an identifier is a nonzero integer of the given width.

    Returns
    -------
    bool
        Whether the value fits the unsigned integer width without accepting
        booleans as integers.
    """
    match identifier:
        case bool():
            return False
        case int() as numeric if 0 < numeric < 1 << width:
            return True
        case _:
            return False


def _increment_trace_count(value: int) -> int:
    """Increment one trace-local counter without exceeding its shared cap.

    Returns
    -------
    int
        The incremented value, capped at the shared maximum.
    """
    return min(MAX_TRACE_COUNT, value + 1)
