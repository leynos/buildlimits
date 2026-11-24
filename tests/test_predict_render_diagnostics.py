"""How the report renders label-loss and observer evidence.

A count that was never measured must not read as a measured zero. These tests
keep the three uninspected states apart in the rendered report: no diagnostics
block, an active-file-only read, and a failed inventory scan. The observer's
own counts are independent evidence, so they must still appear when the
retained-store block is absent.
"""

import typing as typ

from render_helpers import data_section, empty_document

from buildlimits.predict.render import render_markdown


def _diagnostics_document() -> dict[str, object]:
    """Return a document whose store, history and observer counts are measured."""
    document = empty_document()
    data = data_section(document)
    typ.cast("dict[str, object]", data["source"]).update(
        records_total=2,
        outcome_records=2,
        summary_records=0,
    )
    data["diagnostics"] = {
        "store": {
            "records_total": 2,
            "outcome_records": 2,
            "summary_records": 0,
            "by_result": {"hit": 2},
            "by_reason": {"none": 2},
            "by_attribution": {"solo": 2},
            "by_origin": {"registry": 2},
            "by_parser_class": {"cacheable": 2},
            "by_child_sighting": {"unavailable": 2},
            "by_server_version": {"0.14.0": 2},
            "admission_recorded": 2,
            "admission_missing": 0,
            "labels": 2,
            "cacheable_records": 2,
            "cacheable_labels": 2,
        },
        "history": {
            "active_only": False,
            "archived": 1,
            "sealed": 0,
            "gaps": 0,
            "gaps_truncated": False,
            "retention_floor": None,
            "quarantined": 0,
            "inventory_unavailable": False,
        },
    }
    data["journal"] = {
        "available": True,
        "truncated": True,
        "records": 10,
        "span_start": "2026-11-01T00:00:00+00:00",
        "span_end": "2026-11-02T00:00:00+00:00",
        "restarts": {"count": 0, "first": None, "last": None},
        "correlation": "no joinable per-compilation admission record",
        "observer": {
            "requests": 2,
            "outcomes": 2,
            "store_records": 2,
            "store_discarded": 0,
            "readings": 1,
            "readings_skipped": 0,
            "readings_failed": 0,
            "by_cache_path": {"cached": 2},
            "by_backend": {"sccache": 2},
            "by_result": {"hit": 2},
            "by_reason": {"none": 2},
            "by_attribution": {"solo": 2},
            "by_origin": {"registry": 2},
            "by_gate_reason": {},
            "by_reading_reason": {"none": 1},
        },
    }
    return document


def _history_of(document: dict[str, object]) -> dict[str, object]:
    """Return the mutation-friendly history block of a diagnostics document."""
    diagnostics = typ.cast("dict[str, object]", data_section(document)["diagnostics"])
    return typ.cast("dict[str, object]", diagnostics["history"])


def test_a_truncated_journal_marks_its_observer_counts() -> None:
    """Partial observer counts say that the journal cap was reached."""
    markdown = render_markdown(_diagnostics_document())

    assert "the query reached its record cap" in markdown, (
        "the journal note states the cap"
    )
    assert "Observer counts cover only the retained journal window" in markdown, (
        "the observer block warns that its counts are partial"
    )


def _closed_history_line(document: dict[str, object]) -> str:
    """Return the report's one closed-history bullet."""
    markdown = render_markdown(document)
    return next(
        line for line in markdown.splitlines() if line.startswith("- Closed history:")
    )


def test_an_active_only_read_does_not_print_unmeasured_counts() -> None:
    """A file that was never opened is not reported as an empty segment set."""
    document = _diagnostics_document()
    _history_of(document).update(active_only=True, archived=0)

    line = _closed_history_line(document)

    assert line == (
        "- Closed history: not considered (the active file alone was read)"
    ), "the reader is told the history was not examined"


def test_an_unavailable_inventory_says_so_rather_than_printing_zeroes() -> None:
    """A scan failure is unavailable evidence, not a measured empty history."""
    document = _diagnostics_document()
    _history_of(document).update(active_only=False, inventory_unavailable=True)

    line = _closed_history_line(document)

    assert line == (
        "- Closed history: inventory unavailable; rows already read are included"
    ), "the failure is named"


def test_observer_counts_are_rendered_without_a_diagnostics_block() -> None:
    """A pre-accounting snapshot still carries the recorder's own counts."""
    document = _diagnostics_document()
    data_section(document).pop("diagnostics")

    markdown = render_markdown(document)

    assert "Label-loss diagnostics: unavailable" in markdown, "the gap is named"
    assert "- Observer by result: hit=2" in markdown, "the observer still reports"
    assert "Observer counts: unavailable" not in markdown, "they are not called absent"
