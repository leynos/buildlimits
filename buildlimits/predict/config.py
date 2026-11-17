"""Pinned benchmark hyperparameters, fixed before any history is read.

Nothing in the benchmark chooses a parameter at run time. The packaged
``config.json`` is the single source, so a result can be reproduced by checking
out the same commit and reading the same keys.
:mod:`buildlimits.predict.config_loader` reads and validates that file; this
module holds only the value, so the scoring modules depend on a data type
rather than on a JSON reader.
"""

import dataclasses as dc


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
    hedge_weight_floor : float
        Smallest normalized weight kept for a component, so a component that
        was outperformed for a long spell can still win weight back.
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
    hedge_weight_floor: float
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
