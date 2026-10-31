"""Shared test-only helpers for the cache-outcome contracts."""

import datetime as dt
import hashlib
import hmac
import importlib
import json
import typing as typ

from hypothesis import event

if typ.TYPE_CHECKING:
    from types import ModuleType

    from buildlimits.outcome_key import CacheKey


class _TimedGateEvent(typ.Protocol):
    """The monotonic time carried by a fake recorder gate event."""

    now: float


def outcome_module(name: str) -> ModuleType:
    """Load an outcome module lazily so red tests collect before implementation.

    Returns
    -------
    module object
        The requested ``buildlimits`` module, imported on demand.

    Example:
        ``outcome_module("outcome_key").__name__`` is
        ``"buildlimits.outcome_key"`` once the implementation exists.
    """
    return importlib.import_module(f"buildlimits.{name}")


def expected_hmac(value: str, key: bytes) -> str:
    """Return the SHA-256 HMAC expected for a private path input.

    Returns
    -------
    str
        The lowercase hexadecimal HMAC digest.

    Example:
        A 32-byte salt produces a 64-character SHA-256 digest for a path.
    """
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def record_generated_case(name: str) -> None:
    """Record a Hypothesis event that proves a generated branch was reached.

    Example:
        ``record_generated_case("target:path")`` adds that reached branch to
        Hypothesis' generated-case statistics.
    """
    event(name)


GENERATION = outcome_module("outcome_attribution").Generation("run-a", 10, 20)
OBSERVED_AT = dt.datetime(2026, 10, 24, 12, tzinfo=dt.UTC)
_COUNTERS = (
    "compile_requests",
    "requests_unsupported_compiler",
    "requests_not_compile",
    "requests_not_cacheable",
    "requests_executed",
    "cache_hits",
    "cache_misses",
    "cache_errors",
    "non_cacheable_compilations",
    "compilations",
    "compile_fails",
)


class _StatsReading(typ.Protocol):
    """The statistics counter inspected directly by recorder tests."""

    cache_hits: int


def _counter_values(changes: dict[str, int]) -> dict[str, int]:
    """Return the fixed counter shape with supplied non-negative values.

    Returns
    -------
    dict[str, int]
        All sccache counters, with unspecified values set to zero.

    Examples
    --------
    ``_counter_values({"cache_hits": 1})["cache_hits"]`` is ``1``.
    """
    values = dict.fromkeys(_COUNTERS, 0)
    values.update(changes)
    return values


def stats_reading(**changes: int) -> _StatsReading:
    """Build a supported reading with the requested cumulative counters.

    Returns
    -------
    StatsReading
        A supported sccache reading with unspecified counters set to zero.

    Examples
    --------
    ``stats_reading(cache_hits=1).cache_hits`` is ``1``.
    """
    values = _counter_values(changes)
    return outcome_module("outcome_stats").StatsReading(**values, version="0.14.0")


def stats_text(**changes: int) -> str:
    """Encode the pinned sccache JSON boundary around counter values.

    Returns
    -------
    str
        A JSON document using the captured 0.14.0 statistics shape.

    Examples
    --------
    ``'"version":"0.14.0"' in stats_text()`` is ``True``.
    """
    raw: dict[str, object] = {}
    raw.update(_counter_values(changes))
    for field in ("cache_hits", "cache_misses", "cache_errors"):
        raw[field] = {"counts": {"Rust": raw[field]}, "adv_counts": {}}
    return json.dumps({"version": "0.14.0", "stats": raw})


def cache_key() -> CacheKey:
    """Return one stable key whose values contain no local path.

    Returns
    -------
    CacheKey
        A record key using a fixed test salt and registry identity.

    Examples
    --------
    ``cache_key().crate_name`` is ``"serde"``.
    """
    keys = outcome_module("outcome_key")
    salt = keys.Salt(b"s" * 32, "fixed-salt")
    identity = keys.toolchain_identity("/toolchain/rustc", {}, "0.14.0", salt)
    return keys.CacheKey("serde", "registry:serde@1.2.3", "host", "a" * 64, identity)


def launch_payload(recorder: ModuleType, key: object) -> object:
    """Build the immutable metadata retained for one cached launch.

    Returns
    -------
    object
        A launch payload with the supplied key and stable parser metadata.

    Examples
    --------
    ``launch_payload(outcome_module("outcome_recorder"), cache_key()).origin``
    is ``"registry"``.
    """
    return recorder.LaunchPayload(key=key, origin="registry", parser_class="cacheable")


def clear_gate_event(recorder: ModuleType, now: float) -> _TimedGateEvent:
    """Build a gate event with every quiet-point clause satisfied.

    Returns
    -------
    object
        A recorder event carrying clear quiet-point inputs.

    Examples
    --------
    ``clear_gate_event(outcome_module("outcome_recorder"), 1.0)`` is a
    gate-open event at the supplied monotonic time.
    """
    from buildlimits.admission_state import Counts

    gate = outcome_module("outcome_gate")
    inputs = gate.QuietInputs(
        counts=Counts(0, 0, 0, 0, 0, 1, 0),
        is_reachable=True,
        is_cache_allowed=True,
        is_recovery_idle=True,
        has_barrier=False,
        has_query_in_flight=False,
    )
    return recorder.GateOpen(inputs=inputs, now=now)
