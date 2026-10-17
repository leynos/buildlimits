"""Aggregate bounded pressure observations without inventing window counters.

This pure consumer uses validated records only. Lifetime maxima remain lifetime
observations, missing values remain absent, and adapters stay self-reported.
"""

import collections
import typing as typ

from buildlimits.metrics import VOCABULARIES
from buildlimits.metrics_records import of_event, present_samples

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.metrics_records import Record

PRESSURE_FIELDS = frozenset({
    "fd_retained",
    "fd_budget",
    "fd_high_water",
    "queued_high_water",
    "fd_budget_known",
    "accept_paused",
    "accept_pause_seconds",
    "busy_retries",
    "drain_attempts",
    "drain_failures",
    "drain_recoveries",
    "accept_state",
    "accept_errno",
    "drain_operation",
    "drain_state",
    "drain_reason",
})
DESCRIPTOR_FIELDS = frozenset({
    "fd_retained",
    "fd_budget",
    "fd_budget_known",
    "fd_high_water",
})


def pressure_sections(
    records: cabc.Sequence[Record],
    summarize: cabc.Callable[[cabc.Sequence[float]], dict[str, float | int | None]],
) -> dict[str, object]:
    """Add pressure aggregates only when validated pressure observations exist.

    Parameters
    ----------
    records : Sequence[Record]
        Already trusted, bounded and source-filtered journal observations.
    summarize : Callable[[Sequence[float]], dict[str, float | int | None]]
        Existing report percentile function, injected to avoid a module cycle.

    Returns
    -------
    dict[str, object]
        Optional pressure section, or an empty mapping for legacy records.
        Cumulative counters are maxima of observed lifetime snapshots, never
        selected-window sums or inferred deltas across supervisor restarts.
    """
    if not any(
        record.event in {"accept_pressure", "drain_progress"}
        or PRESSURE_FIELDS.intersection(record.fields)
        for record in records
    ):
        return {}
    supervisor = tuple(record for record in records if record.source == "supervisor")
    adapters = tuple(record for record in records if record.source == "adapter")
    return {
        "pressure": {
            "descriptors": _descriptors(supervisor),
            "queue": {
                "maximum_observed_lifetime_high_water": _maximum(
                    supervisor, "queued_high_water"
                ),
            },
            "accept": _accept(supervisor, summarize),
            "drain": _drain(supervisor),
            "adapters": {
                "self_reported": True,
                "busy_retries": summarize(present_samples(adapters, "busy_retries")),
            },
        }
    }


def _maximum(records: cabc.Sequence[Record], field: str) -> int | None:
    """Read the largest observed integer snapshot, retaining absence as null.

    Returns
    -------
    int | None
        Largest present snapshot, or no value when the field is absent.
    """
    return max(
        (int(record.fields[field]) for record in records if field in record.fields),
        default=None,
    )


def _latest(records: cabc.Sequence[Record]) -> Record | None:
    """Select the latest known timestamp; ties and undated sets use journal order.

    Returns
    -------
    Record | None
        Selected observation, or no record for an empty sequence.
    """
    if not records:
        return None
    return max(
        enumerate(records),
        key=lambda pair: (
            pair[1].timestamp.timestamp()
            if pair[1].timestamp is not None
            else -float("inf"),
            pair[0],
        ),
    )[1]


def _descriptors(records: cabc.Sequence[Record]) -> dict[str, object]:
    """Read one current snapshot without stitching missing fields from older ones.

    Returns
    -------
    dict[str, object]
        Latest observed descriptor values and observed lifetime high-water.
    """
    latest = _latest(
        tuple(
            record
            for record in records
            if DESCRIPTOR_FIELDS.intersection(record.fields)
        )
    )
    fields = latest.fields if latest is not None else {}
    return {
        "latest_observed": {
            "retained": int(fields["fd_retained"]) if "fd_retained" in fields else None,
            "budget": int(fields["fd_budget"]) if "fd_budget" in fields else None,
            "budget_known": fields["fd_budget_known"] == "1"
            if "fd_budget_known" in fields
            else None,
        },
        "maximum_observed_lifetime_high_water": _maximum(records, "fd_high_water"),
    }


def _counts(records: cabc.Sequence[Record], field: str) -> dict[str, int]:
    """Count only the fixed field vocabulary present in validated records.

    Returns
    -------
    dict[str, int]
        Positive observation counts keyed by fixed vocabulary tokens.
    """
    counts = collections.Counter(
        record.fields[field] for record in records if field in record.fields
    )
    return {
        token: counts[token] for token in sorted(VOCABULARIES[field]) if counts[token]
    }


def _accept(
    records: cabc.Sequence[Record],
    summarize: cabc.Callable[[cabc.Sequence[float]], dict[str, float | int | None]],
) -> dict[str, object]:
    """Separate completed pause durations from partial failed-resume samples.

    Returns
    -------
    dict[str, object]
        Window transition counts, pause state and distinct elapsed statistics.
    """
    events = of_event(records, "accept_pressure")
    by_state: collections.defaultdict[str | None, list[Record]] = (
        collections.defaultdict(list)
    )
    for record in events:
        by_state[record.fields.get("accept_state")].append(record)
    latest = _latest(events)
    flag = latest.fields.get("accept_paused") if latest is not None else None
    return {
        "window_observations": len(events),
        "states": _counts(events, "accept_state"),
        "errnos": _counts(events, "accept_errno"),
        "latest_observed_paused": flag == "1" if flag is not None else None,
        "completed_pause_seconds": summarize(
            present_samples(by_state["resumed"], "accept_pause_seconds")
        ),
        "resume_failed_elapsed_seconds_maximum": max(
            present_samples(by_state["resume_failed"], "accept_pause_seconds"),
            default=None,
        ),
    }


def _drain(records: cabc.Sequence[Record]) -> dict[str, object]:
    """Keep operation event counts distinct from cumulative counter snapshots.

    Returns
    -------
    dict[str, object]
        Window observations and per-operation maximum lifetime snapshots.
    """
    events = of_event(records, "drain_progress")
    operations = {}
    for operation in sorted(VOCABULARIES["drain_operation"]):
        matching = tuple(
            record
            for record in events
            if record.fields.get("drain_operation") == operation
        )
        if matching:
            operations[operation] = {
                "window_observations": len(matching),
                "states": _counts(matching, "drain_state"),
                "reasons": _counts(matching, "drain_reason"),
                "maximum_observed_lifetime_counters": {
                    name: _maximum(matching, f"drain_{name}")
                    for name in ("attempts", "failures", "recoveries")
                },
            }
    return {"window_observations": len(events), "operations": operations}
