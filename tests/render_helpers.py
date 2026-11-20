"""Report documents shared by the rendering tests.

Every field the renderer reads is present, so a test only has to change the one
field it is about. Building the document once keeps the tests focused on the
sentence or table they check rather than on restating a large literal.
"""

import typing as typ


def empty_document() -> dict[str, object]:
    """Return an insufficient-data report document with no ranked predictor.

    Returns
    -------
    dict[str, object]
        A document with every field the renderer reads and no scored row.
    """
    return {
        "status": "insufficient data",
        "verdict": "insufficient data: 0 labelled events, 2000 required",
        "predictors": {},
        "ranking": [],
        "ablation": {},
        "split": {"train_labelled": 0, "heldout_labelled": 0},
        "replay": {
            "moments": 0,
            "unavailable_admission": 0,
            "applied_labels": 0,
            "unapplied_labels": 0,
        },
        "latency": {
            "file": "timings.json",
            "note": "measured latency; not reproducible byte for byte",
        },
        "data": {
            "stats": {
                "events": 0,
                "labelled": 0,
                "labelled_fraction": 0.0,
                "identities": 0,
                "span_start": None,
                "span_end": None,
                "gaps": [],
                "admission_recorded": 0,
                "admission_usable": 0,
                "admission_missing": 0,
                "admission_unusable": 0,
            },
            "source": {
                "records_total": 0,
                "salt_id": "fixed-salt",
                "written_at": "2026-11-17T12:00:00+00:00",
            },
            "journal": {"available": False, "reason": "journal query failed"},
            "fingerprint": "f" * 64,
        },
        "git_sha": "abc123",
        "config": {"bootstrap_alpha": 0.05},
        "verdict_alpha": 0.05,
    }


def entry(
    *, train: dict[str, object] | None, minimum: bool = True
) -> dict[str, object]:
    """Return one predictor entry with a scored held-out period.

    Parameters
    ----------
    train : dict[str, object] or None
        The training score, or ``None`` for a predictor fitted retrospectively.
    minimum : bool, default=True
        Whether the predictor scored enough held-out rows to be ranked.

    Returns
    -------
    dict[str, object]
        One entry in the report's predictor mapping.
    """
    return {
        "train": train,
        "held-out": {
            "brier": 0.25,
            "log_loss": 0.693147,
            "accuracy": 0.75,
            "coverage": 1.0,
            "scored": 4,
            "calibration": [
                {
                    "lower": 0.0,
                    "upper": 0.5,
                    "count": 2,
                    "mean_probability": 0.25,
                    "observed_rate": 0.5,
                },
                {
                    "lower": 0.5,
                    "upper": 1.0,
                    "count": 2,
                    "mean_probability": 0.75,
                    "observed_rate": 0.5,
                },
            ],
        },
        "against_base_rate": {
            "difference": -0.0125,
            "low": -0.03,
            "high": 0.005,
            "rows": 4,
            "positions": 4,
        },
        "by_origin": {"git": {"brier": 0.2}},
        "meets_minimum": minimum,
    }


def scored_document() -> dict[str, object]:
    """Return a report with one predictor scored in every held-out column.

    Returns
    -------
    dict[str, object]
        An ``ok`` document ranking the ``base-rate`` predictor alone.
    """
    document = empty_document()
    document["status"] = "ok"
    document["verdict"] = "no predictor beats the base rate"
    document["predictors"] = {"base-rate": entry(train={"brier": 0.3})}
    document["ranking"] = ["base-rate"]
    return document


def data_section(document: dict[str, object]) -> dict[str, object]:
    """Return the document's ``data`` mapping, for a test to amend.

    Parameters
    ----------
    document : dict[str, object]
        A report built by `empty_document` or `scored_document`.

    Returns
    -------
    dict[str, object]
        The nested ``data`` mapping, which the caller may mutate.
    """
    return typ.cast("dict[str, object]", document["data"])


def source_section(document: dict[str, object]) -> dict[str, object]:
    """Return the document's ``data.source`` mapping, for a test to amend.

    Parameters
    ----------
    document : dict[str, object]
        A report built by `empty_document` or `scored_document`.

    Returns
    -------
    dict[str, object]
        The nested ``data.source`` mapping, which the caller may mutate.
    """
    return typ.cast("dict[str, object]", data_section(document)["source"])
