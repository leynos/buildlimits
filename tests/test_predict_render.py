"""Report rendering when there is nothing ranked to show."""

import typing as typ

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
