"""Report provenance and limitations: which history a count actually covers."""

from render_helpers import data_section, empty_document, entry, source_section

from buildlimits.predict.render import render_markdown


def test_a_multi_domain_snapshot_names_the_domains_it_counts() -> None:
    """A count spanning several salts cannot be attributed to just the newest."""
    document = empty_document()
    source_section(document)["salt_ids"] = ["salt-a", "salt-b"]

    markdown = render_markdown(document)

    assert (
        "- Store: 0 rows from salt `fixed-salt` across 2 salt domains "
        "(`salt-a`, `salt-b`) written 2026-11-17T12:00:00+00:00"
    ) in markdown, "the provenance names every domain the count covers"


def test_a_long_domain_list_is_bounded() -> None:
    """A damaged store cannot make the provenance line unbounded."""
    document = empty_document()
    source_section(document)["salt_ids"] = [
        "salt-a",
        "salt-b",
        "salt-c",
        "salt-d",
        "salt-e",
    ]

    markdown = render_markdown(document)

    assert (
        "across 5 salt domains (`salt-a`, `salt-b`, `salt-c`, and more)" in markdown
    ), "at most three identifiers are listed"


def test_a_single_domain_snapshot_has_no_domain_note() -> None:
    """One domain needs no explanation and keeps the previous wording."""
    markdown = render_markdown(empty_document())

    assert "salt domains" not in markdown, "a single domain adds no clause"


def test_an_unavailable_journal_states_its_reason() -> None:
    """Provenance names why the journal contributed nothing."""
    markdown = render_markdown(empty_document())

    assert "journal query failed" in markdown, "the reason is rendered"


def test_an_available_journal_reports_its_span_and_its_limit() -> None:
    """A journal that could not be joined says so, and is never scored."""
    document = empty_document()
    data_section(document)["journal"] = {
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
    document = empty_document()
    data_section(document)["journal"] = {
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


def test_the_limitations_state_the_retrospective_exclusion() -> None:
    """A predictor with no training score says why, rather than omitting it."""
    document = empty_document()
    document["predictors"] = {"ensemble-fixed": entry(train=None)}
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
    markdown = render_markdown(empty_document())

    assert "not predicted or scored" in markdown, "a missing admission time is stated"
    assert "not a joinable per-compilation admission record" in markdown, (
        "the journal join limit is stated"
    )
