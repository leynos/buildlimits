"""Timings report rendering: measured latency kept apart from combination."""

from buildlimits.predict.latency import render_timings


def test_the_timings_report_keeps_combination_time_apart() -> None:
    """An ensemble's combination time is never shown as prediction time."""
    entry = {
        "predictions": 2,
        "seconds": {"overall": 0.002, "train": 0.001, "held-out": 0.001},
        "microseconds_each": {"overall": 1000.0, "train": 1000.0, "held-out": 1000.0},
        "seconds_by_origin": {"git": 0.002},
    }
    markdown = render_timings({
        "predictors": {"base-rate": entry},
        "ensembles": {"ensemble-equal": entry},
    })

    before, after = markdown.split("### Ensemble combination latency")
    assert "`base-rate`" in before, "prediction latency leads"
    assert "`ensemble-equal`" not in before, "combination is not prediction"
    assert "Combining only" in after, "the second table says what it measures"
    assert "`ensemble-equal`" in after, "the ensemble is reported apart"


def test_a_scope_with_no_calls_is_missing_not_zero() -> None:
    """A predictor that measured nothing does not report a measured mean."""
    entry = {
        "predictions": 0,
        "seconds": {"overall": 0.0, "train": 0.0, "held-out": 0.0},
        "microseconds_each": {"overall": None, "train": None, "held-out": None},
        "seconds_by_origin": {},
    }
    markdown = render_timings({
        "predictors": {"base-rate": entry},
        "ensembles": {},
    })

    assert "| `base-rate` | 0 | 0.000 | - | - |" in markdown, "empty scope is marked"
