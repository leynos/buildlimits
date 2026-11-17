"""Report rendering: the comparison, calibration and provenance tables."""

import typing as typ

from buildlimits.predict.render import UNDERPOWERED, render_markdown

# The note a run without admission times must print, kept whole so a dropped
# clause fails the test rather than sliding past a substring check.
_ADMISSION_NOTE = (
    "The history is large enough to run, but its records carry no admission "
    "time, so no event could have been predicted before its label was known. "
    "No prediction is reported; the counts describe what is missing."
)


def _empty_document() -> dict[str, object]:
    """Return an insufficient-data report document with no ranked predictor."""
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


def _entry(
    *, train: dict[str, object] | None, minimum: bool = True
) -> dict[str, object]:
    """Return one predictor entry with a scored held-out period."""
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


def _scored_document() -> dict[str, object]:
    """Return a report with one predictor scored in every held-out column."""
    document = _empty_document()
    document["status"] = "ok"
    document["verdict"] = "no predictor beats the base rate"
    document["predictors"] = {"base-rate": _entry(train={"brier": 0.3})}
    document["ranking"] = ["base-rate"]
    return document


def test_a_scored_report_prints_its_tables_exactly() -> None:
    """Each table carries its values in the formats a reader is told to expect.

    The comparison row carries the score, coverage, the paired-row count and
    the corrected interval together; a calibration row names its predictor and
    keeps the count beside two rates; the origin table shares the row prefix.
    """
    markdown = render_markdown(_scored_document())
    sections = {
        "comparison": markdown.split("## Held-out comparison")[1].split("## ")[0],
        "bins": markdown.split("## Calibration on the held-out period")[1].split("## ")[
            0
        ],
        "origin": markdown.split("## Brier by origin (all scored events)")[1],
    }
    rows = {
        key: [
            line for line in section.splitlines() if line.startswith("| `base-rate` |")
        ]
        for key, section in sections.items()
    }

    assert rows == {
        "comparison": [
            (
                "| `base-rate` | 0.2500 | 0.6931 | 0.7500 | 100.0% | 4 | 0.2500 "
                "| -0.0125 | [-0.0300, 0.0050] | - |"
            ),
        ],
        "bins": [
            "| `base-rate` | 0.0000-0.5000 | 2 | 0.2500 | 0.5000 |",
            "| `base-rate` | 0.5000-1.0000 | 2 | 0.7500 | 0.5000 |",
        ],
        "origin": ["| `base-rate` | 0.2000 |"],
    }, "the documented formats, row by row"


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


def test_insufficient_admission_times_are_explained_not_hidden() -> None:
    """A store with no admission time says so rather than reporting a score."""
    document = _empty_document()
    document["status"] = "insufficient admission times"
    document["verdict"] = (
        "insufficient admission times: 400 labelled events, 0 predicted"
    )

    markdown = render_markdown(document)

    assert "**insufficient admission times**" in markdown, "the status is explicit"
    note = markdown.split("> **Insufficient admission times.**")[1].split("\n\n")[0]
    assert note.strip() == _ADMISSION_NOTE, "the note says what is missing and why"


def test_insufficient_held_out_evidence_is_not_a_negative_result() -> None:
    """A run too short to compare is told so, rather than reported as a draw."""
    document = _empty_document()
    document["status"] = "insufficient held-out evidence"
    document["verdict"] = "insufficient held-out evidence: too few paired rows"

    markdown = render_markdown(document)

    assert "**insufficient held-out evidence**" in markdown, "the status is explicit"
    assert "this is not a negative result" in markdown, "the distinction is stated"
    assert "**Insufficient data.**" not in markdown, "nothing is called missing data"


def test_the_difference_table_explains_the_correction() -> None:
    """The reader is told why the interval is wider than the config's level."""
    markdown = render_markdown(_empty_document())

    assert "widened across the family of comparisons" in markdown, (
        "the correction is explained"
    )


def test_the_replay_accounting_is_rendered() -> None:
    """Delivered, missing and unapplied labels are stated, not left implicit."""
    document = _empty_document()
    document["replay"] = {
        "moments": 4,
        "unavailable_admission": 3,
        "applied_labels": 5,
        "unapplied_labels": 2,
    }

    markdown = render_markdown(document)

    assert (
        "- Replay: 4 admission moments, 3 events without a usable admission time, "
        "5 labels delivered before a prediction, 2 labels too late to inform one"
    ) in markdown, "the admission accounting is visible"


def test_the_admission_counters_are_rendered() -> None:
    """Recorded, usable and unusable admission times are counted separately."""
    document = _empty_document()
    stats = typ.cast(
        "dict[str, object]", typ.cast("dict[str, object]", document["data"])["stats"]
    )
    stats.update({
        "admission_recorded": 9,
        "admission_usable": 6,
        "admission_missing": 1,
        "admission_unusable": 2,
    })

    markdown = render_markdown(document)

    assert (
        "- Admission times: 9 recorded, 6 usable for a prediction, 1 missing, "
        "2 recorded after their own label"
    ) in markdown, "the counts say what is missing"


def test_latency_is_pointed_at_rather_than_embedded() -> None:
    """The durable report names the file it keeps measured time in."""
    markdown = render_markdown(_empty_document())

    assert "- Latency: measured separately in `timings.json`" in markdown, (
        "the pointer names the file"
    )


def test_an_empty_history_has_a_missing_window() -> None:
    """No first or last label is shown as missing, not as the text None."""
    markdown = render_markdown(_empty_document())

    assert "- Window: - to -" in markdown, "both ends are marked missing"


def test_an_unavailable_journal_states_its_reason() -> None:
    """Provenance names why the journal contributed nothing."""
    markdown = render_markdown(_empty_document())

    assert "journal query failed" in markdown, "the reason is rendered"


def test_an_available_journal_reports_its_span_and_its_limit() -> None:
    """A journal that could not be joined says so, and is never scored."""
    document = _empty_document()
    data = typ.cast("dict[str, object]", document["data"])
    data["journal"] = {
        "available": True,
        "records": 1234,
        "span_start": "2026-11-01T00:00:00+00:00",
        "span_end": "2026-11-02T00:00:00+00:00",
        "correlation": "no joinable per-compilation admission record",
    }

    markdown = render_markdown(document)

    assert (
        "- Journal: 1,234 records from 2026-11-01T00:00:00+00:00 to "
        "2026-11-02T00:00:00+00:00; "
        "no joinable per-compilation admission record"
    ) in markdown, "the span and the join limit are both stated"


def test_the_journal_reports_the_supervisor_starts_it_witnessed() -> None:
    """Restart times the journal saw are reported, not inferred for the store."""
    document = _empty_document()
    data = typ.cast("dict[str, object]", document["data"])
    data["journal"] = {
        "available": True,
        "records": 40,
        "span_start": "2026-11-01T00:00:00+00:00",
        "span_end": "2026-11-02T00:00:00+00:00",
        "restarts": {
            "count": 2,
            "first": "2026-11-01T06:00:00+00:00",
            "last": "2026-11-01T18:00:00+00:00",
        },
        "correlation": "no joinable per-compilation admission record",
    }

    markdown = render_markdown(document)

    assert "2 supervisor starts between 2026-11-01T06:00:00+00:00 and " in markdown, (
        "the restart times are rendered"
    )


def test_the_interval_column_follows_the_corrected_level() -> None:
    """A 90% per-comparison level is labelled as such, not hard-coded."""
    document = _empty_document()
    document["verdict_alpha"] = 0.1

    markdown = render_markdown(document)

    assert (
        "| Predictor | Brier | Log loss | Accuracy | Coverage | n | "
        "Calibration error | Difference vs base rate | 90.0% interval | Notes |"
    ) in markdown, "the header is derived from the level actually applied"


def test_an_under_powered_predictor_is_flagged_in_its_row() -> None:
    """A predictor with too few scored rows is shown, and marked as such."""
    document = _empty_document()
    document["predictors"] = {"thin": _entry(train=None, minimum=False)}
    document["ranking"] = ["thin"]

    markdown = render_markdown(document)

    assert UNDERPOWERED in markdown, "the flag is rendered"
    assert f"| {UNDERPOWERED} |" in markdown, "and sits in the row's notes column"


def test_calibration_is_rendered_for_every_scored_predictor() -> None:
    """The leader is not the only predictor whose calibration is shown."""
    document = _empty_document()
    document["predictors"] = {
        "base-rate": _entry(train={"brier": 0.3}),
        "last-outcome": _entry(train=None),
    }
    document["ranking"] = ["base-rate", "last-outcome"]

    markdown = render_markdown(document)

    section = markdown.split("## Calibration on the held-out period")[1].split("## ")[0]
    assert "| `base-rate` |" in section, "the leader is shown"
    assert "| `last-outcome` |" in section, "and so is every other scored predictor"


def test_the_limitations_state_the_retrospective_exclusion() -> None:
    """A predictor with no training score says why, rather than omitting it."""
    document = _empty_document()
    document["predictors"] = {"ensemble-fixed": _entry(train=None)}
    document["ranking"] = ["ensemble-fixed"]

    markdown = render_markdown(document)

    section = markdown.split("## Limitations")[1]
    assert (
        "- The fixed ensemble's weights were fitted on the tuning labels, so "
        "its tuning rows are excluded rather than reported as retrospective "
        "predictions: `ensemble-fixed`."
    ) in section, "the affected predictor is named with the reason"


def test_the_limitations_state_the_missing_admission_and_join_limits() -> None:
    """The report never lets a reader assume an invented decision time."""
    markdown = render_markdown(_empty_document())

    assert "not predicted or scored" in markdown, "a missing admission time is stated"
    assert "not a joinable per-compilation admission record" in markdown, (
        "the journal join limit is stated"
    )


def test_the_rendered_report_is_stable() -> None:
    """The same document renders the same bytes."""
    document = _empty_document()

    assert render_markdown(document) == render_markdown(document), "deterministic"
