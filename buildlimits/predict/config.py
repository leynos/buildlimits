"""Pinned benchmark hyperparameters, loaded from one versioned JSON file.

Nothing in the benchmark chooses a parameter at run time. The file beside this
module is the single source, so a result can be reproduced by checking out the
same commit and reading the same keys.
"""

import dataclasses as dc
import json
import typing as typ
from pathlib import Path

CONFIG_VERSION = 1
CONFIG_PATH = Path(__file__).with_name("config.json")


@dc.dataclass(frozen=True, slots=True)
class Config:
    """Every benchmark parameter, fixed before any history is read.

    Attributes
    ----------
    cold_start : float
        Probability used before any labelled outcome is known.
    beta_window : int
        Outcomes in the Beta component's recency window.
    beta_prior_strength : float
        Total pseudo-count in the Beta prior; split around the origin rate.
    rolling_window : int
        Outcomes in the rolling-window component.
    ewma_alpha : float
        Weight of the newest outcome in the exponentially weighted component.
    logistic_learning_rate : float
        Online gradient step for the feature model.
    logistic_l2 : float
        L2 penalty applied to the feature model's weights.
    hedge_eta : float
        Learning rate of the adaptive ensemble's exponentiated weights.
    weight_fit_steps : int
        Projected-gradient steps used to fit fixed ensemble weights.
    weight_fit_learning_rate : float
        Step size of that projected gradient.
    train_fraction : float
        Share of events that may inform tuning or weight fitting.
    block_size : int
        Consecutive events in one bootstrap block.
    bootstrap_resamples : int
        Bootstrap replications behind every interval.
    bootstrap_seed : int
        Seed shared by every bootstrap draw.
    bootstrap_alpha : float
        Two-sided tail probability of the reported interval.
    log_loss_clip : float
        Probability clip that keeps log loss finite.
    decision_threshold : float
        Threshold behind the reported accuracy.
    minimum_labelled_events : int
        Labelled events required before a verdict is claimed.
    minimum_scored_events : int
        Scored rows required before a predictor is ranked or selected.
    load_window_seconds : float
        Trailing window that defines the load feature.
    gap_threshold_seconds : float
        Silence long enough to be reported as a history gap.
    calibration_bins : int
        Fixed probability bins in the calibration report.
    """

    config_version: int
    cold_start: float
    beta_window: int
    beta_prior_strength: float
    rolling_window: int
    ewma_alpha: float
    logistic_learning_rate: float
    logistic_l2: float
    hedge_eta: float
    weight_fit_steps: int
    weight_fit_learning_rate: float
    train_fraction: float
    block_size: int
    bootstrap_resamples: int
    bootstrap_seed: int
    bootstrap_alpha: float
    log_loss_clip: float
    decision_threshold: float
    minimum_labelled_events: int
    minimum_scored_events: int
    load_window_seconds: float
    gap_threshold_seconds: float
    calibration_bins: int

    def as_dict(self) -> dict[str, object]:
        """Return the pinned parameters as a JSON-ready mapping.

        Returns
        -------
        dict[str, object]
            Every parameter under its file name.
        """
        return dc.asdict(self)


def load_config(path: Path | None = None) -> Config:
    """Read and validate one parameter file.

    Parameters
    ----------
    path : Path | None
        File to read; the packaged ``config.json`` when absent.

    Returns
    -------
    Config
        The pinned parameters.

    Raises
    ------
    TypeError
        If the file does not hold a JSON object.
    ValueError
        If the file names an unknown key, omits one, or has a version this
        code does not read.

    Examples
    --------
    The packaged file loads without an explicit path:

    >>> load_config().beta_window
    8
    """
    source = CONFIG_PATH if path is None else path
    raw = typ.cast("object", json.loads(source.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        msg = f"{source} must hold one JSON object"
        raise TypeError(msg)
    value = typ.cast("dict[str, object]", raw)
    fields = {field.name for field in dc.fields(Config)}
    missing = fields - value.keys()
    unknown = value.keys() - fields
    if missing or unknown:
        msg = f"{source} has missing {sorted(missing)} and unknown {sorted(unknown)}"
        raise ValueError(msg)
    config = Config(**typ.cast("dict[str, typ.Any]", value))
    if config.config_version != CONFIG_VERSION:
        msg = f"{source} has unsupported version {config.config_version}"
        raise ValueError(msg)
    return config
