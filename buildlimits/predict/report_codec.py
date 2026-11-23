"""Encode the report's values as the deterministic JSON shapes.

The scores, intervals, split and the snapshot's counts are domain values; their
report shape is a serialization concern, so the encoders live here rather than
on the values. :mod:`buildlimits.predict.report` calls these functions and
keeps the document's layout in one place, and
:mod:`buildlimits.predict.snapshot_codec` reuses them so a snapshot document
and a report describe the same data block identically.
"""

import dataclasses as dc
import typing as typ

from buildlimits.predict.accounting import (
    Diagnostics,
    HistoryLoss,
    StoreAccounting,
    checked_counts,
)
from buildlimits.utc_text import optional_utc_text, utc_text

if typ.TYPE_CHECKING:
    from buildlimits.predict.bootstrap import Interval
    from buildlimits.predict.config import Config
    from buildlimits.predict.dataset import Gap, SnapshotStats, Source
    from buildlimits.predict.harness import Split
    from buildlimits.predict.metrics import CalibrationBin, Metrics

_STORE_COUNT_FIELDS = (
    "records_total",
    "outcome_records",
    "summary_records",
    "admission_recorded",
    "admission_missing",
    "labels",
    "cacheable_records",
    "cacheable_labels",
)
_STORE_MAP_FIELDS = (
    "by_result",
    "by_reason",
    "by_attribution",
    "by_origin",
    "by_parser_class",
    "by_child_sighting",
    "by_server_version",
)
_HISTORY_COUNT_FIELDS = ("archived", "sealed", "gaps", "quarantined")


def config_to_dict(config: Config) -> dict[str, object]:
    """Return the pinned parameters as the report's JSON-ready mapping.

    Returns
    -------
    dict[str, object]
        Every parameter under its file name.
    """
    return dc.asdict(config)


def calibration_bin_to_dict(bin_: CalibrationBin) -> dict[str, object]:
    """Return the report shape for one calibration bin.

    Returns
    -------
    dict[str, object]
        The bin under stable keys.
    """
    return {
        "lower": bin_.lower,
        "upper": bin_.upper,
        "count": bin_.count,
        "mean_probability": bin_.mean_probability,
        "observed_rate": bin_.observed_rate,
    }


def metrics_to_dict(metrics: Metrics) -> dict[str, object]:
    """Return the deterministic report shape for one predictor's scores.

    Returns
    -------
    dict[str, object]
        Every metric under stable keys.
    """
    return {
        "labelled": metrics.labelled,
        "scored": metrics.scored,
        "abstentions": metrics.abstentions,
        "coverage": metrics.coverage,
        "brier": metrics.brier,
        "log_loss": metrics.log_loss,
        "accuracy": metrics.accuracy,
        "calibration": [calibration_bin_to_dict(bin_) for bin_ in metrics.calibration],
    }


def interval_to_dict(interval: Interval) -> dict[str, object]:
    """Return the report shape for one paired interval.

    Returns
    -------
    dict[str, object]
        The interval under stable keys.
    """
    return {
        "difference": interval.difference,
        "low": interval.low,
        "high": interval.high,
        "resamples": interval.resamples,
        "block_size": interval.block_size,
        "blocks": interval.blocks,
        "rows": interval.rows,
        "positions": interval.positions,
    }


def source_to_dict(source: Source) -> dict[str, object]:
    """Return the snapshot shape for the store provenance.

    Returns
    -------
    dict[str, object]
        The provenance under stable keys.
    """
    document: dict[str, object] = {
        "kind": "cache-outcomes",
        "salt_id": source.salt_id,
        "written_at": utc_text(source.written_at),
        "records_total": source.records_total,
        "outcome_records": source.outcome_records,
        "summary_records": source.summary_records,
    }
    # Only a history that spans several domains needs the extra field, so a
    # single-domain snapshot keeps exactly the document earlier versions wrote.
    if len(source.salt_ids) > 1:
        document["salt_ids"] = list(source.salt_ids)
    return document


def stats_to_dict(stats: SnapshotStats) -> dict[str, object]:
    """Return the snapshot shape for the history's summary counts.

    Returns
    -------
    dict[str, object]
        Every count and span under stable keys.
    """
    return {
        "events": stats.events,
        "labelled": stats.labelled,
        "labelled_fraction": stats.labelled_fraction,
        "identities": stats.identities,
        "span_start": optional_utc_text(stats.span_start),
        "span_end": optional_utc_text(stats.span_end),
        "admission_recorded": stats.admission_recorded,
        "admission_usable": stats.admission_usable,
        "admission_missing": stats.admission_missing,
        "admission_unusable": stats.admission_unusable,
        "gaps": [gap_to_dict(gap) for gap in stats.gaps],
    }


def gap_to_dict(gap: Gap) -> dict[str, object]:
    """Return the snapshot shape for one reported silence.

    Returns
    -------
    dict[str, object]
        The gap under stable keys.
    """
    return {
        "start": utc_text(gap.start),
        "end": utc_text(gap.end),
        "seconds": round(gap.seconds, 3),
    }


def split_to_dict(split: Split) -> dict[str, object]:
    """Return the report shape for the tuning/held-out split.

    Returns
    -------
    dict[str, object]
        Counts and the boundary timestamp.
    """
    return {
        "train_labelled": split.train_labelled,
        "heldout_labelled": split.heldout_labelled,
        "boundary": (None if split.boundary is None else utc_text(split.boundary)),
    }


def diagnostics_to_dict(diagnostics: Diagnostics | None) -> dict[str, object] | None:
    """Return the snapshot shape for the label-loss diagnostics.

    Returns
    -------
    dict[str, object] | None
        The diagnostics, or None when the snapshot carries none.
    """
    if diagnostics is None:
        return None
    store = diagnostics.store
    history = diagnostics.history
    return {
        "store": {
            "records_total": store.records_total,
            "outcome_records": store.outcome_records,
            "summary_records": store.summary_records,
            "admission_recorded": store.admission_recorded,
            "admission_missing": store.admission_missing,
            "labels": store.labels,
            "cacheable_records": store.cacheable_records,
            "cacheable_labels": store.cacheable_labels,
            **{name: dict(getattr(store, name)) for name in _STORE_MAP_FIELDS},
        },
        "history": {
            "active_only": history.active_only,
            "archived": history.archived,
            "sealed": history.sealed,
            "gaps": history.gaps,
            "gaps_truncated": history.gaps_truncated,
            "retention_floor": history.retention_floor,
            "quarantined": history.quarantined,
        },
    }


def diagnostics_from_dict(value: object, *, label: str) -> Diagnostics:
    """Validate one decoded diagnostics block and restore its counts.

    Every breakdown must sum to the per-compilation row count, so a corrupted
    or hand-edited snapshot is refused rather than reported as evidence.

    Returns
    -------
    Diagnostics
        The validated counts.

    Raises
    ------
    TypeError
        If the block or one of its top-level values has the wrong Python type.
    """
    if not isinstance(value, dict):
        msg = f"{label} diagnostics must be an object"
        raise TypeError(msg)
    block = typ.cast("dict[str, object]", value)
    store_value = block.get("store")
    history_value = block.get("history")
    if not isinstance(store_value, dict) or not isinstance(history_value, dict):
        msg = f"{label} diagnostics must hold store and history objects"
        raise TypeError(msg)
    store = _store_accounting_from(typ.cast("dict[str, object]", store_value), label)
    return Diagnostics(store=store, history=_history_loss_from(history_value, label))


def _store_accounting_from(value: dict[str, object], label: str) -> StoreAccounting:
    """Validate the retained-store counts and their reconciliation.

    Returns
    -------
    StoreAccounting
        The validated store counts.

    Raises
    ------
    ValueError
        If the totals do not reconcile.
    """
    counts = {
        name: _required_count(value.get(name), label=f"{label} {name}")
        for name in _STORE_COUNT_FIELDS
    }
    maps = {
        name: checked_counts(value.get(name), label=f"{label} {name}")
        for name in _STORE_MAP_FIELDS
    }
    outcome_records = counts["outcome_records"]
    if counts["records_total"] != outcome_records + counts["summary_records"]:
        msg = f"{label} totals do not reconcile"
        raise ValueError(msg)
    if any(sum(counts_by.values()) != outcome_records for counts_by in maps.values()):
        msg = f"{label} breakdowns do not sum to the outcome rows"
        raise ValueError(msg)
    if counts["admission_recorded"] + counts["admission_missing"] != outcome_records:
        msg = f"{label} admission counts do not sum to the outcome rows"
        raise ValueError(msg)
    labels = counts["labels"]
    if labels != maps["by_result"].get("hit", 0) + maps["by_result"].get("miss", 0):
        msg = f"{label} labelled count disagrees with its result breakdown"
        raise ValueError(msg)
    cacheable = counts["cacheable_records"]
    cacheable_labels = counts["cacheable_labels"]
    if cacheable != maps["by_parser_class"].get("cacheable", 0):
        msg = f"{label} cacheable count disagrees with its parser breakdown"
        raise ValueError(msg)
    if not cacheable_labels <= cacheable <= outcome_records:
        msg = f"{label} cacheable counts are inconsistent"
        raise ValueError(msg)
    if cacheable_labels > labels:
        msg = f"{label} cacheable labels exceed all labels"
        raise ValueError(msg)
    return StoreAccounting(
        records_total=counts["records_total"],
        outcome_records=outcome_records,
        summary_records=counts["summary_records"],
        admission_recorded=counts["admission_recorded"],
        admission_missing=counts["admission_missing"],
        labels=labels,
        cacheable_records=cacheable,
        cacheable_labels=cacheable_labels,
        by_result=maps["by_result"],
        by_reason=maps["by_reason"],
        by_attribution=maps["by_attribution"],
        by_origin=maps["by_origin"],
        by_parser_class=maps["by_parser_class"],
        by_child_sighting=maps["by_child_sighting"],
        by_server_version=maps["by_server_version"],
    )


def _history_loss_from(value: dict[str, object], label: str) -> HistoryLoss:
    """Validate one decoded history-loss block.

    Returns
    -------
    HistoryLoss
        The validated loss statement.

    Raises
    ------
    TypeError
        If a flag has the wrong Python type.
    """
    active_only = value.get("active_only")
    gaps_truncated = value.get("gaps_truncated")
    if not isinstance(active_only, bool) or not isinstance(gaps_truncated, bool):
        msg = f"{label} history flags must be booleans"
        raise TypeError(msg)
    retention_floor = value.get("retention_floor")
    if retention_floor is not None:
        retention_floor = _required_count(
            retention_floor, label=f"{label} retention_floor"
        )
    return HistoryLoss(
        active_only=active_only,
        archived=_required_count(value.get("archived"), label=f"{label} archived"),
        sealed=_required_count(value.get("sealed"), label=f"{label} sealed"),
        gaps=_required_count(value.get("gaps"), label=f"{label} gaps"),
        gaps_truncated=gaps_truncated,
        retention_floor=retention_floor,
        quarantined=_required_count(
            value.get("quarantined"), label=f"{label} quarantined"
        ),
    )


def _required_count(value: object, *, label: str) -> int:
    """Read one required non-negative integer count.

    Returns
    -------
    int
        The validated count.

    Raises
    ------
    TypeError
        If the value is not an integer.
    ValueError
        If the value is a boolean or is negative.
    """
    match value:
        case bool():
            msg = f"{label} must be a non-negative integer"
            raise ValueError(msg)
        case int() if value < 0:
            msg = f"{label} must be a non-negative integer"
            raise ValueError(msg)
        case int():
            return value
        case _:
            msg = f"{label} must be an integer"
            raise TypeError(msg)
