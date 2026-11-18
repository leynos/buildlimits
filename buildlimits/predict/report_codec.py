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

from buildlimits.utc_text import optional_utc_text, utc_text

if typ.TYPE_CHECKING:
    from buildlimits.predict.bootstrap import Interval
    from buildlimits.predict.config import Config
    from buildlimits.predict.dataset import Gap, SnapshotStats, Source
    from buildlimits.predict.harness import Split
    from buildlimits.predict.metrics import CalibrationBin, Metrics


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
    return {
        "kind": "cache-outcomes",
        "salt_id": source.salt_id,
        "written_at": utc_text(source.written_at),
        "records_total": source.records_total,
        "outcome_records": source.outcome_records,
        "summary_records": source.summary_records,
    }


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
