"""Pure aggregation of bounded supervisor and self-reported adapter records."""

import collections
import math
import statistics
import typing as typ

from buildlimits.metrics_pressure_report import pressure_sections
from buildlimits.metrics_records import of_event, present_samples

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.metrics_query import Record, Window


def counts_by(records: cabc.Sequence[Record], field: str) -> dict[str, int]:
    """Count validated tokens, without creating labels from absent fields.

    Parameters
    ----------
    records : Sequence[Record]
        Already validated metric records.
    field : str
        A schema field to aggregate.

    Returns
    -------
    dict[str, int]
        Sorted counts for present tokens.
    """
    return dict(
        sorted(
            collections.Counter(
                record.fields[field] for record in records if field in record.fields
            ).items()
        )
    )


def percentiles(values: cabc.Sequence[float]) -> dict[str, float | int | None]:
    """Compute nearest-rank p50, p90, p99 and maximum, without interpolation.

    Parameters
    ----------
    values : Sequence[float]
        Bounded non-negative measurements.

    Returns
    -------
    dict[str, float | int | None]
        Sample count and rounded statistics; absent samples yield ``None``.
    """
    ordered = sorted(values)
    return {
        "count": len(ordered),
        **{
            f"p{rank}": round(ordered[math.ceil(len(ordered) * rank / 100) - 1], 3)
            if ordered
            else None
            for rank in (50, 90, 99)
        },
        "maximum": round(ordered[-1], 3) if ordered else None,
    }


def report(window: Window) -> dict[str, object]:
    """Summarize a window without blending adapter and supervisor observations.

    Parameters
    ----------
    window : Window
        Validated records and their configured lookback duration.

    Returns
    -------
    dict[str, object]
        Counts, rates, distributions, gauge samples and bounded history.
    """
    supervisor = tuple(r for r in window.records if r.source == "supervisor")
    adapters = tuple(r for r in window.records if r.source == "adapter")
    times = sorted(r.timestamp for r in window.records if r.timestamp is not None)
    return {
        "window_seconds": window.seconds,
        "records": len(window.records),
        "truncated": window.truncated,
        "first_timestamp": times[0].isoformat() if times else None,
        "last_timestamp": times[-1].isoformat() if times else None,
        "requests": _requests(supervisor, window.seconds),
        "gauges": _gauges(supervisor),
        "cache": _cache(supervisor),
        "transitions": _transitions(supervisor),
        "adapters": {
            "self_reported": True,
            "outcomes": counts_by(adapters, "outcome"),
            "connection_attempts": percentiles(
                present_samples(adapters, "connection_attempts")
            ),
            "wait_seconds": percentiles(present_samples(adapters, "wait_seconds")),
            "runtime_seconds": percentiles(
                present_samples(adapters, "runtime_seconds")
            ),
        },
        "history": [
            {
                "timestamp": r.timestamp.isoformat()
                if r.timestamp is not None
                else None,
                "source": r.source,
                "self_reported": r.source == "adapter",
                "event": r.event,
                "fields": dict(r.fields),
            }
            for r in sorted(
                window.records,
                key=lambda r: (
                    r.timestamp.timestamp() if r.timestamp is not None else -math.inf
                ),
            )
        ],
        **pressure_sections(window.records, percentiles),
    }


def _requests(records: cabc.Sequence[Record], seconds: int) -> dict[str, object]:
    """Count terminal requests once and use the requested window for rates.

    Returns
    -------
    dict[str, object]
        Terminal counts and duration distributions.
    """
    finished = of_event(records, "request_finished")
    return {
        "outcomes": counts_by(finished, "outcome"),
        "reasons": counts_by(finished, "reason"),
        "cancellations": counts_by(finished, "cancellation"),
        "per_minute": round(len(finished) * 60 / seconds, 6),
        "wait_seconds": percentiles(present_samples(finished, "wait_seconds")),
        "runtime_seconds": percentiles(present_samples(finished, "runtime_seconds")),
    }


def _gauges(records: cabc.Sequence[Record]) -> dict[str, object]:
    """Aggregate gauge samples without implying a time-weighted queue mean.

    Returns
    -------
    dict[str, object]
        Queue maximum, arithmetic sample mean and held maximum.
    """
    queued, held = present_samples(records, "queued"), present_samples(records, "held")
    return {
        "queue_maximum": max(queued) if queued else None,
        "queue_sample_mean": round(statistics.mean(queued), 3) if queued else None,
        "held_maximum": max(held) if held else None,
    }


def _cache(records: cabc.Sequence[Record]) -> dict[str, object]:
    """Count admission cache paths and fallbacks among sccache-eligible groups.

    Returns
    -------
    dict[str, object]
        Cache-path counts and fallback ratio; no eligible groups means ``None``.
    """
    paths = counts_by(of_event(records, "request_admitted"), "cache_path")
    eligible = paths.get("cached", 0) + paths.get("direct_fallback", 0)
    return {
        "paths": paths,
        "direct_fallback_ratio": round(paths.get("direct_fallback", 0) / eligible, 6)
        if eligible
        else None,
    }


def _transitions(records: cabc.Sequence[Record]) -> dict[str, object]:
    """Count health, cache, pool and restart events already deduplicated at source.

    Returns
    -------
    dict[str, object]
        Bounded transition and failure counts.
    """
    events = collections.Counter(r.event for r in records)
    return {
        "reachability": counts_by(of_event(records, "cache_reachability"), "reachable"),
        "cache_load_unknown": counts_by(
            of_event(records, "cache_load_unknown"), "load_unknown"
        ),
        "pool_failures": counts_by(of_event(records, "pool_failure"), "operation"),
        "health": counts_by(of_event(records, "health_changed"), "health"),
        "reachability_transitions": events["cache_reachability"],
        "cache_load_unknown_transitions": events["cache_load_unknown"],
        "pool_failure_count": events["pool_failure"],
        "health_transitions": events["health_changed"],
        "supervisor_starts": events["supervisor_started"],
        "internal_errors": events["internal_error"],
        "server_drain_released": sum(
            int(r.fields.get("released", "0"))
            for r in records
            if r.event == "server_drain_released"
        ),
    }
