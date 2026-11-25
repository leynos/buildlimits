"""The report's data-sufficiency block: what the labels rest on.

The block is deliberately about the input, not the scores. It names the
observer's denominators, the retained rows, the labelled share, the
admission-time coverage, the labels withheld for an unverified time, and the
closed-history loss, so a reader can tell a negative result from a history that
never carried enough evidence. It lives apart from the comparison tables in
:mod:`buildlimits.predict.render_tables` because it reads the snapshot's data
block rather than the result document's scores.
"""

import typing as typ

from buildlimits.predict.formatting import (
    TRUNCATED_OBSERVER_NOTE,
    admission_line,
    as_count,
    as_counts,
    as_percent,
    generation_unavailable_line,
)
from buildlimits.predict.report_blocks import report_data

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def data_sufficiency(document: cabc.Mapping[str, object]) -> list[str]:
    """Return the counts that explain what the labels rest on.

    Returns
    -------
    list[str]
        Markdown lines.
    """
    blocks = report_data(document)
    stats = blocks.stats
    source = blocks.source
    journal = blocks.journal
    lines = [
        "## Data sufficiency",
        "",
        _rows_read_line(stats, source),
        (
            f"- Labels: {as_count(stats['labelled'])} of "
            f"{as_count(stats['events'])} events "
            f"({as_percent(stats['labelled_fraction'])}), "
            f"{as_count(stats['identities'])} distinct identities"
        ),
        _unverified_line(stats),
        admission_line(stats),
    ]
    diagnostics = blocks.diagnostics
    if diagnostics is None:
        lines.append(
            "- Label-loss diagnostics: unavailable; this snapshot predates "
            "the retained-store accounting."
        )
    else:
        lines += _diagnostics_lines(typ.cast("dict[str, object]", diagnostics))
    # The journal's observer counts are independent of the retained-store
    # block, so they are still worth printing when the store counts are not.
    lines += _observer_lines(journal)
    lines += [
        "",
        (
            "An ambiguous or unavailable window remains `unknown`; the report "
            "never turns a missing hit into a miss."
        ),
        "",
    ]
    return lines


def _diagnostics_lines(diagnostics: cabc.Mapping[str, object]) -> list[str]:
    """Return the retained-store and closed-history bullets.

    Returns
    -------
    list[str]
        Markdown lines.
    """
    store = typ.cast("dict[str, object]", diagnostics["store"])
    history = typ.cast("dict[str, object]", diagnostics["history"])
    return [
        (
            f"- Label coverage among outcome rows: {as_count(store['labels'])} of "
            f"{as_count(store['outcome_records'])}"
        ),
        (
            f"- Label coverage among cacheable compilations: "
            f"{as_count(store.get('cacheable_labels'))} of "
            f"{as_count(store.get('cacheable_records'))}"
        ),
        f"- By cache result: {as_counts(store['by_result'])}",
        f"- By outcome reason: {as_counts(store['by_reason'])}",
        f"- By attribution: {as_counts(store['by_attribution'])}",
        f"- By origin: {as_counts(store['by_origin'])}",
        f"- By parser class: {as_counts(store['by_parser_class'])}",
        f"- Label loss {generation_unavailable_line()}",
        _closed_history_line(history),
    ]


def _closed_history_line(history: cabc.Mapping[str, object]) -> str:
    """Return one honest statement of what closed history the snapshot saw.

    A count that was never measured must not read as a measured zero, so the
    two uninspected cases name themselves instead of printing counts. The
    measured branch names the quarantined files as well, because the plain
    command report already prints that count and a reader comparing the two
    reports must not see one of them omit evidence the snapshot holds.

    Returns
    -------
    str
        A measured counts line, or the reason no count is available.
    """
    uninspected = _UNINSPECTED_HISTORY.get(_history_state(history))
    if uninspected is not None:
        return uninspected
    return (
        f"- Closed history: archived {as_count(history['archived'])}, "
        f"sealed {as_count(history['sealed'])}, "
        f"gaps {as_count(history['gaps'])}, "
        f"quarantined {as_count(history['quarantined'])}"
        + (" (gap list truncated)" if history.get("gaps_truncated") else "")
    )


# The two states in which closed history was not measured, so no count may be
# printed as one. A state outside this table was measured.
_UNINSPECTED_HISTORY = {
    "active_only": "- Closed history: not considered (the active file alone was read)",
    "inventory_unavailable": (
        "- Closed history: inventory unavailable; rows already read are included"
    ),
}


def _history_state(history: cabc.Mapping[str, object]) -> str:
    """Return which unmeasured state the history block reports, if any.

    ``active_only`` takes precedence, so a block that somehow claims both is
    described as the narrower read rather than the failed scan.

    Returns
    -------
    str
        ``active_only``, ``inventory_unavailable``, or ``measured``.
    """
    states = ("active_only", "inventory_unavailable")
    return next((state for state in states if history.get(state)), "measured")


def _rows_read_line(
    stats: cabc.Mapping[str, object], source: cabc.Mapping[str, object]
) -> str:
    """Return the row-count line, tolerating a document without source counts.

    Returns
    -------
    str
        The Markdown bullet naming outcome and summary rows.
    """
    events = stats.get("events")
    return (
        f"- Rows read: {as_count(source.get('records_total', events))} "
        f"({as_count(source.get('outcome_records', events))} outcome rows, "
        f"{as_count(source.get('summary_records', 0))} window summaries)"
    )


def _unverified_line(stats: cabc.Mapping[str, object]) -> str:
    """Return the bullet naming labels withheld for an unverified label time.

    A snapshot written before the provenance marker existed carries no count,
    so an absent value reads as the dash rather than a measured zero. The
    sentence states the consequence as well: those labels are neither released
    nor scored, which is why the label gate can be met by fewer usable rows.

    Returns
    -------
    str
        The Markdown bullet.
    """
    return (
        f"- Labels withheld for an unverified label time: "
        f"{as_count(stats.get('unverified_label_time'))}; they are neither "
        "released nor scored."
    )


def _omission_line(block: cabc.Mapping[str, object]) -> str:
    """Return the bullet accounting for published outcomes without a store row.

    A published outcome is not simply an appended row or an omitted one. A
    failed append lands in ``store_discarded`` with a reason, a successful
    reading appends its outcome rows *and* a window summary, and a write that
    was queued inside the window may complete after it. The identity the bullet
    states is therefore the one the counts can prove, and it says outright that
    a reading's summary is not an outcome row.

    Returns
    -------
    str
        The Markdown bullet.
    """
    unrecorded = block.get("record_omitted_unrecorded")
    omitted = (
        f"{as_count(block.get('record_omitted'))}, by reason "
        f"{as_counts(block.get('by_omission_reason'))}"
    )
    if unrecorded:
        omitted = (
            f"{omitted}; {as_count(unrecorded)} published outcomes carried no "
            "omission field, so their omission is unknown rather than measured"
        )
    return (
        f"- Observer outcomes without a store row: {omitted}. Published outcomes "
        "are accounted for by appended outcome rows, pre-store omissions, "
        "discarded writes and outstanding queued writes; a reading's window "
        "summary is not an outcome row"
    )


def _observer_lines(journal: cabc.Mapping[str, object]) -> list[str]:
    """Return the recorder's own counts from the journal note.

    Returns
    -------
    list[str]
        Markdown lines, or an explicit absence note.
    """
    observer = journal.get("observer")
    if observer is None or not isinstance(observer, dict):
        return [
            (
                "- Observer counts: unavailable; the snapshot carries no "
                "observer-count block."
            )
        ]
    block = typ.cast("dict[str, object]", observer)
    lines = [
        (
            f"- Observer: {as_count(block.get('requests'))} admitted requests, "
            f"{as_count(block.get('outcomes'))} published outcomes, "
            f"{as_count(block.get('store_records'))} rows appended, "
            f"{as_count(block.get('store_discarded'))} dropped or evicted"
        ),
        _omission_line(block),
        (
            f"- Observer readings: {as_count(block.get('readings'))} completed, "
            f"{as_count(block.get('readings_skipped'))} skipped, "
            f"{as_count(block.get('readings_failed'))} failed"
        ),
        f"- Observer by cache path: {as_counts(block.get('by_cache_path'))}",
        f"- Observer by result: {as_counts(block.get('by_result'))}",
        f"- Observer by reason: {as_counts(block.get('by_reason'))}",
        (
            f"- Observer by store operation: "
            f"{as_counts(block.get('by_store_operation'))}; only an append adds "
            "a row, a compaction reports the rows it retained and a seal the "
            "rows it released"
        ),
        (f"- Observer by store reason: {as_counts(block.get('by_store_reason'))}"),
        (f"- Observer by gate reason: {as_counts(block.get('by_gate_reason'))}"),
    ]
    if journal.get("truncated"):
        lines.append(f"- {TRUNCATED_OBSERVER_NOTE}")
    return lines
