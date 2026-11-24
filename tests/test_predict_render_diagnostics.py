"""How the report renders label-loss and observer evidence.

A count that was never measured must not read as a measured zero. These tests
keep the three uninspected states apart in the rendered report: no diagnostics
block, an active-file-only read, and a failed inventory scan. The observer's
own counts are independent evidence, so they must still appear when the
retained-store block is absent.
"""

import typing as typ

from render_helpers import data_section, empty_document

from buildlimits.predict.formatting import TRUNCATED_OBSERVER_NOTE
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
            "record_omitted": 0,
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
            "by_omission_reason": {},
            "by_store_operation": {"append": 1},
            "by_store_reason": {},
            "by_gate_reason": {},
            "by_reading_reason": {"none": 1},
        },
    }
    return document


def _nested(document: dict[str, object], *path: str) -> dict[str, object]:
    """Return a mutation-friendly block reached by ``path`` from ``data``.

    Returns
    -------
    dict[str, object]
        The nested mapping, so a test can amend one field in place.
    """
    block: dict[str, object] = data_section(document)
    for name in path:
        block = typ.cast("dict[str, object]", block[name])
    return block


def _history_of(document: dict[str, object]) -> dict[str, object]:
    """Return the mutation-friendly history block of a diagnostics document."""
    return _nested(document, "diagnostics", "history")


def test_a_truncated_journal_marks_its_observer_counts() -> None:
    """Partial observer counts say that the journal cap was reached."""
    markdown = render_markdown(_diagnostics_document())

    assert "the query reached its record cap" in markdown, (
        "the journal note states the cap"
    )
    assert f"- {TRUNCATED_OBSERVER_NOTE}" in markdown, (
        "the observer block warns that its counts are partial"
    )


def test_a_complete_journal_prints_no_partial_observer_warning() -> None:
    """A window that never reached its cap is printed without the warning."""
    document = _diagnostics_document()
    typ.cast("dict[str, object]", data_section(document)["journal"])["truncated"] = (
        False
    )

    markdown = render_markdown(document)

    assert TRUNCATED_OBSERVER_NOTE not in markdown, "a complete window is not marked"
    assert "- Observer by result: hit=2" in markdown, "its counts are still printed"


def test_the_store_reason_breakdown_is_rendered() -> None:
    """Retention loss and failed writes are distinguished in the observer block."""
    document = _diagnostics_document()
    observer = typ.cast(
        "dict[str, object]",
        typ.cast("dict[str, object]", data_section(document)["journal"])["observer"],
    )
    observer["store_discarded"] = 5
    observer["by_store_reason"] = {"expired": 4, "save_failed": 1}

    markdown = render_markdown(document)

    assert "- Observer by store reason: expired=4, save_failed=1" in markdown, (
        "each reason keeps its own count"
    )


def test_the_report_states_how_many_label_times_it_could_not_verify() -> None:
    """A withheld label is named with its count and its consequence."""
    document = _diagnostics_document()
    stats = typ.cast("dict[str, object]", data_section(document)["stats"])
    stats["unverified_label_time"] = 3

    markdown = render_markdown(document)

    assert (
        "- Labels withheld for an unverified label time: 3; they are neither "
        "released nor scored." in markdown
    ), "the withheld count and its consequence are printed"


def _line_starting(markdown: str, prefix: str) -> str:
    """Return the one report line that begins with ``prefix``.

    Returns
    -------
    str
        The matching line, so a test can compare it whole rather than probe
        for a substring. A report that prints no such line raises
        ``StopIteration``, which the test framework reports as a failure.
    """
    return next(line for line in markdown.splitlines() if line.startswith(prefix))


def test_an_older_snapshot_prints_a_dash_for_the_unverified_count() -> None:
    """A document written before the count existed is not a measured zero."""
    document = _diagnostics_document()
    stats = typ.cast("dict[str, object]", data_section(document)["stats"])
    stats.pop("unverified_label_time")

    line = _line_starting(
        render_markdown(document),
        "- Labels withheld for an unverified label time:",
    )

    assert ": -;" in line, "an absent count is a dash, never a measured zero"


def _observer_block(document: dict[str, object]) -> dict[str, object]:
    """Return a document's mutable observer-count block.

    Returns
    -------
    dict[str, object]
        The observer counts nested under ``data.journal``.
    """
    return _nested(document, "journal", "observer")


def test_the_observer_states_omitted_outcomes_and_their_reasons() -> None:
    """An outcome published without a row is counted by cause, not lost."""
    document = _diagnostics_document()
    observer = _observer_block(document)
    observer["record_omitted"] = 1
    observer["by_omission_reason"] = {"key_unavailable": 1}

    line = _line_starting(
        render_markdown(document),
        "- Observer outcomes without a store row:",
    )

    assert line == (
        "- Observer outcomes without a store row: 1, by reason "
        "key_unavailable=1; a published outcome reconciles as an appended row "
        "plus an omitted one"
    ), "the omitted total, its reason and the reconciliation rule are stated"


def test_the_observer_names_the_operations_that_are_not_appends() -> None:
    """A compaction and a seal are visible without being called appends."""
    document = _diagnostics_document()
    _observer_block(document)["by_store_operation"] = {
        "append": 5,
        "compact": 2,
        "seal": 1,
    }
    markdown = render_markdown(document)

    line = _line_starting(markdown, "- Observer by store operation:")

    assert line == (
        "- Observer by store operation: append=5, compact=2, seal=1; only an "
        "append adds a row, a compaction reports the rows it retained and a "
        "seal the rows it released"
    ), "every operation is named and the append rule is stated"
    assert "records appended" not in markdown, "no label calls a mixed total appends"


def test_the_missing_generation_dimension_is_named() -> None:
    """A dimension no row can carry is stated as unavailable, with its reason."""
    markdown = render_markdown(_diagnostics_document())

    line = next(
        line for line in markdown.splitlines() if "by generation: unavailable" in line
    )
    assert "sccache version" in line and "server generation" in line, (
        "the reason names what the row does carry"
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
