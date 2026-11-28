"""Report rendering: the comparison, calibration and accounting output."""

import typing as typ

from render_helpers import data_section, empty_document, entry, scored_document

from buildlimits.predict.render import UNDERPOWERED, render_markdown

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

# The note a run without admission times must print, kept whole so a dropped
# clause fails the test rather than sliding past a substring check.
_ADMISSION_NOTE = (
    "The history is large enough to run, but its records carry no admission "
    "time, so no event could have been predicted before its label was known. "
    "No prediction is reported; the counts describe what is missing."
)


def test_a_scored_report_prints_its_tables_exactly() -> None:
    """Each table carries its values in the formats a reader is told to expect.

    The comparison row carries the score, coverage, the paired-row count and
    the corrected interval together; a calibration row names its predictor and
    keeps the count beside two rates; the origin table shares the row prefix.
    """
    markdown = render_markdown(scored_document())
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


def test_the_report_states_its_label_loss_denominators(
    snapshot: SnapshotAssertion,
) -> None:
    """The sufficiency block names the rows, labels and refusal reasons.

    The measured closed-history bullet carries the quarantined file count as
    well as the archived, sealed and gap counts, so the Markdown report states
    the same evidence the plain command report prints.
    """
    document = empty_document()
    data = data_section(document)
    typ.cast("dict[str, object]", data["source"]).update(
        records_total=9,
        outcome_records=8,
        summary_records=1,
    )
    data["diagnostics"] = {
        "store": {
            "records_total": 9,
            "outcome_records": 8,
            "summary_records": 1,
            "by_result": {"hit": 2, "miss": 1, "unattributed": 5},
            "by_reason": {"none": 3, "mixed_window": 5},
            "by_attribution": {"solo": 3, "none": 5},
            "by_origin": {"registry": 8},
            "by_parser_class": {"cacheable": 8},
            "by_child_sighting": {"unavailable": 8},
            "by_server_version": {"0.14.0": 8},
            "admission_recorded": 3,
            "admission_missing": 5,
            "labels": 3,
            "cacheable_records": 8,
            "cacheable_labels": 3,
        },
        "history": {
            "active_only": False,
            "archived": 4,
            "sealed": 1,
            "gaps": 2,
            "gaps_truncated": False,
            "retention_floor": 7,
            "quarantined": 2,
        },
    }

    markdown = render_markdown(document)

    section = _data_sufficiency_section(markdown)
    assert section == snapshot, "the sufficiency block is a stable input contract"
    assert "- Closed history: archived 4, sealed 1, gaps 2, quarantined 2" in section, (
        "the quarantined count is stated beside the other measured counts"
    )


def _data_sufficiency_section(markdown: str) -> str:
    """Return the report's data-sufficiency block as one bounded string.

    Returns
    -------
    str
        The heading and bullets up to the next top-level heading. The block is
        a stable input contract, so a formatting or count change is visible.
    """
    lines = markdown.splitlines()
    start = lines.index("## Data sufficiency")
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("## ")
        ),
        len(lines),
    )
    return "\n".join(lines[start:end]).strip()


def test_an_unranked_report_omits_the_detail_tables() -> None:
    """With no scored predictor there is no calibration or origin table."""
    markdown = render_markdown(empty_document())

    assert "Calibration on the held-out period" not in markdown, "no calibration"
    assert "Brier by origin" not in markdown, "no origin table"
    assert "| Predictor | Brier |" in markdown, "the empty comparison is shown"


def test_an_unranked_report_still_states_the_data_gap() -> None:
    """The reviewer is told why the numbers are missing."""
    markdown = render_markdown(empty_document())

    assert "**insufficient data**" in markdown, "the status is explicit"
    assert (
        "> **Insufficient data.** These numbers describe the history that is "
        "available; no synthetic result is substituted."
    ) in markdown, "the warning says both that history is missing and why"


def test_insufficient_admission_times_are_explained_not_hidden() -> None:
    """A store with no admission time says so rather than reporting a score."""
    document = empty_document()
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
    document = empty_document()
    document["status"] = "insufficient held-out evidence"
    document["verdict"] = "insufficient held-out evidence: too few paired rows"

    markdown = render_markdown(document)

    assert "**insufficient held-out evidence**" in markdown, "the status is explicit"
    assert "this is not a negative result" in markdown, "the distinction is stated"
    assert "**Insufficient data.**" not in markdown, "nothing is called missing data"


def test_the_difference_table_explains_the_correction() -> None:
    """The reader is told why the interval is wider than the config's level."""
    markdown = render_markdown(empty_document())

    assert "widened across the family of comparisons" in markdown, (
        "the correction is explained"
    )


def test_the_replay_accounting_is_rendered() -> None:
    """Delivered, missing and unapplied labels are stated, not left implicit."""
    document = empty_document()
    document["replay"] = {
        "moments": 4,
        "unavailable_admission": 3,
        "unavailable_availability": 1,
        "applied_labels": 5,
        "unapplied_labels": 2,
    }

    markdown = render_markdown(document)

    assert (
        "- Replay: 4 admission moments, 3 events without a usable admission time, "
        "1 events without a verified label-availability time, "
        "5 labels delivered before a prediction, 2 labels too late to inform one"
    ) in markdown, "the admission accounting is visible"


def test_the_admission_counters_are_rendered() -> None:
    """Recorded, usable and unusable admission times are counted separately."""
    document = empty_document()
    stats = typ.cast("dict[str, object]", data_section(document)["stats"])
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
    markdown = render_markdown(empty_document())

    assert "- Latency: measured separately in `timings.json`" in markdown, (
        "the pointer names the file"
    )


def test_an_empty_history_has_a_missing_window() -> None:
    """No first or last label is shown as missing, not as the text None."""
    markdown = render_markdown(empty_document())

    assert "- Window: - to -" in markdown, "both ends are marked missing"


def test_the_interval_column_follows_the_corrected_level() -> None:
    """A 90% per-comparison level is labelled as such, not hard-coded."""
    document = empty_document()
    document["verdict_alpha"] = 0.1

    markdown = render_markdown(document)

    assert (
        "| Predictor | Brier | Log loss | Accuracy | Coverage | n | "
        "Calibration error | Difference vs base rate | 90.0% interval | Notes |"
    ) in markdown, "the header is derived from the level actually applied"


def test_an_under_powered_predictor_is_flagged_in_its_row() -> None:
    """A predictor with too few scored rows is shown, and marked as such."""
    document = empty_document()
    document["predictors"] = {"thin": entry(train=None, minimum=False)}
    document["ranking"] = ["thin"]

    markdown = render_markdown(document)

    assert UNDERPOWERED in markdown, "the flag is rendered"
    assert f"| {UNDERPOWERED} |" in markdown, "and sits in the row's notes column"


def test_calibration_is_rendered_for_every_scored_predictor() -> None:
    """The leader is not the only predictor whose calibration is shown."""
    document = empty_document()
    document["predictors"] = {
        "base-rate": entry(train={"brier": 0.3}),
        "last-outcome": entry(train=None),
    }
    document["ranking"] = ["base-rate", "last-outcome"]

    markdown = render_markdown(document)

    section = markdown.split("## Calibration on the held-out period")[1].split("## ")[0]
    assert "| `base-rate` |" in section, "the leader is shown"
    assert "| `last-outcome` |" in section, "and so is every other scored predictor"


def test_the_rendered_report_is_stable() -> None:
    """The same document renders the same bytes."""
    document = empty_document()

    assert render_markdown(document) == render_markdown(document), "deterministic"
