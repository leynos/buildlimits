"""Encode and decode the snapshot's bounded label-loss diagnostics.

The writer and the loader disagreeing about one count would make a snapshot
that the command produced unreadable by the benchmark, so both directions live
here: the encoder states every field the report prints, and the decoder
reconciles the totals before any of them can be trusted.
"""

import typing as typ

from buildlimits.predict.accounting import (
    Diagnostics,
    HistoryLoss,
    StoreAccounting,
    checked_counts,
)

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
    _require_cacheable(
        cacheable,
        cacheable_labels,
        labels,
        outcome_records,
        maps["by_parser_class"],
        label,
    )
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


def _require_cacheable(  # ruff: ignore[too-many-arguments,too-many-positional-arguments]  # one value per reconcilable count; pylint: disable=too-many-arguments,too-many-positional-arguments
    cacheable: int,
    cacheable_labels: int,
    labels: int,
    outcome_records: int,
    parser_counts: dict[str, int],
    label: str,
) -> None:
    """Require the cacheable counts to describe a possible history.

    Raises
    ------
    ValueError
        If the counts disagree with the parser breakdown, exceed the rows they
        are drawn from, or fall below the intersection the two subsets must
        share.
    """
    if cacheable != parser_counts.get("cacheable", 0):
        msg = f"{label} cacheable count disagrees with its parser breakdown"
        raise ValueError(msg)
    if not cacheable_labels <= cacheable <= outcome_records:
        msg = f"{label} cacheable counts are inconsistent"
        raise ValueError(msg)
    if cacheable_labels > labels:
        msg = f"{label} cacheable labels exceed all labels"
        raise ValueError(msg)
    # Two subsets of the outcome rows must intersect by at least this much.
    # A smaller value describes a history that cannot exist.
    if cacheable_labels < max(0, labels + cacheable - outcome_records):
        msg = f"{label} cacheable labels are below the possible intersection"
        raise ValueError(msg)


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
