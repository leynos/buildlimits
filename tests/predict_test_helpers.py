"""Shared builders for the prediction benchmark's tests."""

import dataclasses as dc
import json
import typing as typ

from buildlimits.predict.config import load_config

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.predict.config import Config


def predict_config(**changes: object) -> Config:
    """Return the pinned config with test-speed overrides applied.

    The bootstrap is small and the verdict thresholds are low, so a behavioural
    test can walk a synthetic stream quickly. ``changes`` overrides any
    parameter after those defaults.

    Returns
    -------
    Config
        The parameters to run one test with.
    """
    base = dc.replace(
        load_config(),
        minimum_labelled_events=100,
        minimum_scored_events=1,
        bootstrap_resamples=100,
    )
    return dc.replace(base, **changes)


def write_json(path: Path, document: object) -> Path:
    """Write one JSON document as text and return its path.

    Returns
    -------
    Path
        The path that was written.
    """
    path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    return path
