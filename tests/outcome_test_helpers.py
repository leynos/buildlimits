"""Shared test-only helpers for the cache-outcome contracts."""

import datetime as dt
import hashlib
import hmac
import importlib
import json
import typing as typ

from hypothesis import event

from buildlimits.outcome_record_format import OutcomeRecord, WindowSummary

if typ.TYPE_CHECKING:
    from types import ModuleType

    from buildlimits.outcome_key import CacheKey
    from buildlimits.outcome_vocabulary import CacheResult


class _TimedGateEvent(typ.Protocol):
    """The times carried by a fake recorder gate event."""

    now: float
    observed_at: dt.datetime | None


def window_summary(
    *,
    server_version: str = "0.14.0",
    observed_at: dt.datetime = dt.datetime(2026, 10, 25, tzinfo=dt.UTC),
) -> WindowSummary:
    """Build one small valid row for store-writer contracts.

    Returns
    -------
    WindowSummary
        A balanced one-group summary with fixed test values.

    Examples
    --------
    ``window_summary().window_groups`` is one.
    """
    return WindowSummary(
        observed_at=observed_at,
        server_version=server_version,
        window_groups=1,
        requests_not_compile=0,
        requests_not_cacheable=0,
        predicted_not_compile=0,
        predicted_declined=0,
        predicted_cacheable=1,
    )


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


def attributed_record(
    *,
    observed_at: dt.datetime = OBSERVED_AT,
    outcome: CacheResult = "hit",
) -> OutcomeRecord:
    """Build one attributed registry record with fixed test values.

    Parameters
    ----------
    observed_at : datetime.datetime
        When the compilation outcome became known.
    outcome : str
        The recorded cache outcome; an unattributed value stays in the record
        so a test can check that it never becomes a label or a miss.

    Returns
    -------
    OutcomeRecord
        A cacheable registry record observed at ``observed_at``.
    """
    return OutcomeRecord(
        key=cache_key(),
        outcome=outcome,
        origin="registry",
        runtime_seconds=0.25,
        observed_at=observed_at,
        parser_class="cacheable",
        child_sighting="unavailable",
        child_samples=0,
    )


def records_at(observed: dt.datetime, count: int) -> tuple[OutcomeRecord, ...]:
    """Build ``count`` records one minute apart from ``observed``.

    Parameters
    ----------
    observed : datetime.datetime
        The first record's observation time.
    count : int
        How many records to build.

    Returns
    -------
    tuple[OutcomeRecord, ...]
        The records, oldest first.
    """
    return tuple(
        attributed_record(observed_at=observed + dt.timedelta(minutes=index))
        for index in range(count)
    )


def records_around_a_cutoff(
    now: dt.datetime,
) -> tuple[tuple[OutcomeRecord, ...], dt.datetime]:
    """Build *at cutoff, expired, fresh* records and the cutoff between them.

    The expired record sits between two eligible ones, so a byte allowance
    that counts policy drops has to measure a row it has not reached yet.

    Parameters
    ----------
    now : datetime.datetime
        The instant the expired record was observed.

    Returns
    -------
    tuple[tuple[OutcomeRecord, ...], datetime.datetime]
        The records in stored order and the cutoff that expires only the
        second of them.
    """
    records = (
        attributed_record(observed_at=now + dt.timedelta(minutes=1)),
        attributed_record(observed_at=now),
        attributed_record(observed_at=now + dt.timedelta(minutes=2)),
    )
    return records, now + dt.timedelta(minutes=1)


STORE_EPOCH = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)


def record_after(minute: int, *, now: dt.datetime = STORE_EPOCH) -> OutcomeRecord:
    """Build one attributed record observed ``minute`` minutes after ``now``.

    Returns
    -------
    OutcomeRecord
        A cacheable record observed at the offset.

    Examples
    --------
    ``record_after(0).observed_at`` is ``STORE_EPOCH``.
    """
    return attributed_record(observed_at=now + dt.timedelta(minutes=minute))


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


def clear_gate_event(
    recorder: ModuleType,
    now: float,
    *,
    observed_at: dt.datetime = OBSERVED_AT,
) -> _TimedGateEvent:
    """Build a gate event with every quiet-point clause satisfied.

    ``observed_at`` is the wall-clock moment the gate opened, which the
    supervisor shell always supplies.

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
    return recorder.GateOpen(inputs=inputs, now=now, observed_at=observed_at)
