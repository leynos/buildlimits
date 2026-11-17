"""Report rendering when there is nothing ranked to show."""

import typing as typ

from buildlimits.predict.latency import render_timings
from buildlimits.predict.render import render_markdown


def _empty_document() -> dict[str, object]:
    """Return an insufficient-data report document with no ranked predictor."""
    return {
        "status": "insufficient data",
        "verdict": "insufficient data: 0 labelled events, 2000 required",
        "predictors": {},
        "ranking": [],
        "ablation": {},
        "split": {"train_labelled": 0, "heldout_labelled": 0},
        "data": {
            "stats": {
                "events": 0,
                "labelled": 0,
                "labelled_fraction": 0.0,
                "identities": 0,
                "span_start": None,
                "span_end": None,
                "gaps": [],
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
    }


def test_an_unranked_report_omits_the_detail_tables() -> None:
    """With no scored predictor there is no calibration or origin table."""
    markdown = render_markdown(_empty_document())

    assert "Calibration on the held-out period" not in markdown, "no calibration"
    assert "Brier by origin" not in markdown, "no origin table"
    assert "| Predictor | Brier |" in markdown, "the empty comparison is shown"


def test_an_unranked_report_still_states_the_data_gap() -> None:
    """The reviewer is told why the numbers are missing."""
    markdown = render_markdown(_empty_document())

    assert "**insufficient data**" in markdown, "the status is explicit"
    assert (
        "> **Insufficient data.** These numbers describe the history that is "
        "available; no synthetic result is substituted."
    ) in markdown, "the warning says both that history is missing and why"


def test_insufficient_held_out_evidence_is_not_a_negative_result() -> None:
    """A run too short to compare is told so, rather than reported as a draw."""
    document = _empty_document()
    document["status"] = "insufficient held-out evidence"
    document["verdict"] = "insufficient held-out evidence: too few paired rows"

    markdown = render_markdown(document)

    assert "**insufficient held-out evidence**" in markdown, "the status is explicit"
    assert "this is not a negative result" in markdown, "the distinction is stated"
    assert "**Insufficient data.**" not in markdown, "nothing is called missing data"


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


def test_an_empty_history_has_a_missing_window() -> None:
    """No first or last label is shown as missing, not as the text None."""
    markdown = render_markdown(_empty_document())

    assert "- Window: - to -" in markdown, "both ends are marked missing"


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


def test_an_unavailable_journal_states_its_reason() -> None:
    """Provenance names why the journal contributed nothing."""
    markdown = render_markdown(_empty_document())

    assert "journal query failed" in markdown, "the reason is rendered"


def test_an_available_journal_is_reported_as_context_only() -> None:
    """Journal records are never presented as scored history."""
    document = _empty_document()
    data = typ.cast("dict[str, object]", document["data"])
    data["journal"] = {"available": True, "records": 1234}

    markdown = render_markdown(document)

    assert "1,234 records, context only" in markdown, "counted, not scored"


def test_the_interval_column_follows_the_configured_alpha() -> None:
    """A 90% two-sided interval is labelled as such, not hard-coded."""
    document = _empty_document()
    document["config"] = {"bootstrap_alpha": 0.1}

    markdown = render_markdown(document)

    assert "| Predictor | Difference | 90.0% interval | Paired rows |" in markdown, (
        "the header is derived from the alpha"
    )


def test_the_rendered_report_is_stable() -> None:
    """The same document renders the same bytes."""
    document = _empty_document()

    assert render_markdown(document) == render_markdown(document), "deterministic"
