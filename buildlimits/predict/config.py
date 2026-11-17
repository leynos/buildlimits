"""Pinned benchmark hyperparameters, loaded from one versioned JSON file.

Nothing in the benchmark chooses a parameter at run time. The file beside this
module is the single source, so a result can be reproduced by checking out the
same commit and reading the same keys.
"""

import dataclasses as dc
import json
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

CONFIG_VERSION = 1
CONFIG_PATH = Path(__file__).with_name("config.json")
_INT_FIELDS = frozenset({
    "config_version",
    "beta_window",
    "rolling_window",
    "weight_fit_steps",
    "block_size",
    "bootstrap_resamples",
    "bootstrap_seed",
    "minimum_labelled_events",
    "minimum_scored_events",
    "calibration_bins",
})
# Every parameter has a range that keeps it meaningful; a value outside one is
# a corrupt file, not a tuning choice. The bounds are inclusive.
_BOUNDS: dict[str, tuple[float, float]] = {
    "config_version": (CONFIG_VERSION, CONFIG_VERSION),
    "cold_start": (0.0, 1.0),
    "beta_window": (1, 10_000),
    "beta_prior_strength": (0.0, 1_000_000.0),
    "rolling_window": (1, 10_000),
    "ewma_alpha": (0.0, 1.0),
    "logistic_learning_rate": (0.0, 1.0),
    "logistic_l2": (0.0, 1.0),
    "hedge_eta": (0.0, 10.0),
    "hedge_weight_floor": (0.0, 0.5),
    "weight_fit_steps": (1, 100_000),
    "weight_fit_learning_rate": (0.0, 10.0),
    "train_fraction": (0.0, 1.0),
    "block_size": (1, 1_000_000),
    "bootstrap_resamples": (1, 100_000),
    "bootstrap_seed": (0, 2**63 - 1),
    "bootstrap_alpha": (0.0, 1.0),
    "log_loss_clip": (0.0, 0.5),
    "decision_threshold": (0.0, 1.0),
    "minimum_labelled_events": (1, 1_000_000_000),
    "minimum_scored_events": (1, 1_000_000_000),
    "load_window_seconds": (0.0, 86400.0),
    "gap_threshold_seconds": (0.0, 31_536_000.0),
    "calibration_bins": (1, 1_000),
}
# A range floor some parameters must strictly exceed. ``1.0 - 2**-54`` rounds
# back to ``1.0``, so a clip that small leaves the upper tail unclipped and a
# certain miss would take the logarithm of zero. ``2**-53`` still yields the
# largest float below one, so it is the smallest accepted clip.
_EXCLUSIVE_LOW: dict[str, float] = {
    "log_loss_clip": 2.0**-54,
    # A floor of zero is no floor, and the adaptive update would keep the
    # weight it drove to zero there for the rest of the stream.
    "hedge_weight_floor": 0.0,
}


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

    Notes
    -----
    The validator rejects a file that names an unknown key, omits one, or has
    a version this code does not read with ``ValueError``; see
    :func:`_validated`.

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
    return _validated(source, typ.cast("dict[str, object]", raw))


def _validated(source: Path, value: cabc.Mapping[str, object]) -> Config:
    """Check the version, the key set and every value, then build the config.

    The version is read before the key set so that a file written by a later
    build reports its version rather than a list of keys this build does not
    know.

    Returns
    -------
    Config
        The validated parameters.

    Raises
    ------
    TypeError
        If the version is not an integer, or a value has the wrong type.
    ValueError
        If the version is unknown, a key is missing or unknown, or a value is
        outside its allowed range.
    """
    version = value.get("config_version")
    if not _is_int(version):
        msg = f"{source} must name an integer config_version"
        raise TypeError(msg)
    if version != CONFIG_VERSION:
        msg = f"{source} has unsupported version {version}"
        raise ValueError(msg)
    fields = {field.name for field in dc.fields(Config)}
    missing = fields - value.keys()
    unknown = value.keys() - fields
    if missing or unknown:
        msg = f"{source} has missing {sorted(missing)} and unknown {sorted(unknown)}"
        raise ValueError(msg)
    checked = {name: _checked_value(source, name, raw) for name, raw in value.items()}
    return Config(**typ.cast("dict[str, typ.Any]", checked))


def _is_int(value: object) -> bool:
    """Return whether a JSON value is an integer rather than a bool.

    ``bool`` is an ``int`` subclass, so ``true`` would otherwise pass for a
    count.

    Returns
    -------
    bool
        Whether the value is a real integer.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _checked_value(source: Path, name: str, raw: object) -> int | float:
    """Return one parameter after checking its type and range.

    Returns
    -------
    int | float
        The parameter, converted to a float when its field is fractional.

    Raises
    ------
    TypeError
        If the value is of the wrong type.
    ValueError
        If the value is outside its allowed range.
    """
    if name in _INT_FIELDS:
        if not _is_int(raw):
            msg = f"{source} field {name} must be an integer, not {raw!r}"
            raise TypeError(msg)
        number: int | float = typ.cast("int", raw)
    else:
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            msg = f"{source} field {name} must be a number, not {raw!r}"
            raise TypeError(msg)
        number = float(typ.cast("int | float", raw))
    low, high = _BOUNDS[name]
    if name in _EXCLUSIVE_LOW:
        floor = _EXCLUSIVE_LOW[name]
        if not floor < number <= high:
            msg = (
                f"{source} field {name} must be above {floor} and at most "
                f"{high}, not {number}"
            )
            raise ValueError(msg)
    elif not low <= number <= high:
        msg = f"{source} field {name} must be within [{low}, {high}], not {number}"
        raise ValueError(msg)
    return number
