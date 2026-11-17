"""Encode the report's scored values as the deterministic JSON shapes.

The scores, intervals and split are domain values; their report shape is a
serialization concern, so the encoders live here rather than on the values.
:mod:`buildlimits.predict.report` calls these functions and keeps the document's
layout in one place.
"""

import typing as typ

from buildlimits.utc_text import utc_text

if typ.TYPE_CHECKING:
    from buildlimits.predict.bootstrap import Interval
    from buildlimits.predict.harness import Split
    from buildlimits.predict.metrics import CalibrationBin, Metrics


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
